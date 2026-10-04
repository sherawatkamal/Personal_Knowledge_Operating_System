"""Sync engine with an in-memory connector: idempotency, crash safety, deletion by diff."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from pkos import db, migrate
from pkos.config import REPO_ROOT
from pkos.connectors.base import Change
from pkos.episodes import Episode, store
from pkos.episodes.normalize import build_body
from pkos.sync import sync_source

T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)


class FakeSource:
    """Diff-based connector over an in-memory dict, like Granola: full listing each run."""

    source = "granola"

    def __init__(self, items: dict[str, str], fail_after: int | None = None):
        self.items = items
        self.fail_after = fail_after
        self.version = 0

    def changes(self, watermark, known) -> Iterator[Change]:
        for ext_id, deleted in known.items():
            if not deleted and ext_id not in self.items:
                yield Change(ext_id, None)
        for n, (ext_id, text) in enumerate(sorted(self.items.items())):
            if self.fail_after is not None and n == self.fail_after:
                raise ConnectionError("network dropped mid-sync")
            body, sections = build_body([("transcript", text, False)])
            yield Change(
                ext_id,
                Episode(self.source, ext_id, T0, body=body, sections=sections, raw={"v": n}),
            )
        self.version += 1

    def new_watermark(self):
        return {"version": self.version}


@pytest.fixture
def conn(fresh_db):
    with db.connect(fresh_db, autocommit=True) as c:
        migrate.migrate(c, REPO_ROOT / "migrations")
        yield c


def count(conn, live=False):
    view = "live_episodes" if live else "episodes"
    return conn.execute(f"SELECT count(*) FROM {view}").fetchone()[0]


def snapshot(conn):
    return conn.execute("SELECT * FROM episodes ORDER BY id").fetchall()


def test_sync_twice_writes_nothing_the_second_time(conn):
    src = FakeSource({"a": "one", "b": "two", "c": "three"})
    first = sync_source(conn, src, 30)
    assert first.counts["inserted"] == 3
    before = snapshot(conn)
    second = sync_source(conn, src, 30)
    assert second.writes == 0
    assert second.counts["unchanged"] == 3
    assert snapshot(conn) == before


def test_crash_mid_sync_keeps_progress_and_does_not_advance_watermark(conn):
    src = FakeSource({"a": "one", "b": "two", "c": "three"}, fail_after=2)
    with pytest.raises(ConnectionError):
        sync_source(conn, src, 30)
    assert count(conn) == 2, "changes applied before the crash are committed"
    assert store.get_watermark(conn, "granola") == {}, "watermark must not move on failure"

    src.fail_after = None
    rerun = sync_source(conn, src, 30)
    assert rerun.counts == {
        "inserted": 1,
        "updated": 0,
        "restored": 0,
        "unchanged": 2,
        "tombstoned": 0,
    }
    assert count(conn) == 3
    assert store.get_watermark(conn, "granola") == {"version": 1}


def test_upstream_deletion_tombstones_and_reappearance_restores(conn):
    src = FakeSource({"a": "one", "b": "two"})
    sync_source(conn, src, 30)
    del src.items["b"]
    r = sync_source(conn, src, 30)
    assert r.counts["tombstoned"] == 1
    assert count(conn) == 2 and count(conn, live=True) == 1, "soft delete: row kept, not live"
    assert sync_source(conn, src, 30).writes == 0, "tombstoning is idempotent"

    src.items["b"] = "two"
    assert sync_source(conn, src, 30).counts["restored"] == 1
    assert count(conn, live=True) == 2


def test_sync_never_purges_but_reports_eligible(conn):
    src = FakeSource({"a": "one"})
    sync_source(conn, src, 30)
    src.items.clear()
    sync_source(conn, src, 30)
    conn.execute("UPDATE episodes SET deleted_at = now() - interval '40 days'")
    r = sync_source(conn, src, 30)
    assert r.purge_eligible == 1
    assert count(conn) == 1, "S1: sync reports purge candidates and deletes nothing"


def test_deletion_diff_is_scoped_to_its_source(conn):
    body, sections = build_body([("body", "an email", False)])
    store.upsert(conn, Episode("gmail", "m1", T0, body=body, sections=sections))
    sync_source(conn, FakeSource({}), 30)
    assert count(conn, live=True) == 1, "an empty granola listing must not touch gmail rows"


def test_connector_yielding_foreign_source_is_rejected(conn):
    class Wrong(FakeSource):
        def changes(self, watermark, known):
            yield Change("x", Episode("gmail", "x", T0))

    with pytest.raises(ValueError, match="yielded a gmail episode"):
        sync_source(conn, Wrong({}), 30)
