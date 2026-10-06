"""Idempotent upsert, tombstones and restore: the core of 'sync twice must not duplicate'."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from pkos import db, migrate
from pkos.config import REPO_ROOT
from pkos.episodes import Episode, Participant, store
from pkos.episodes.normalize import build_body
from pkos.episodes.store import Outcome

T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)


@pytest.fixture
def conn(fresh_db):
    with db.connect(fresh_db, autocommit=True) as c:
        migrate.migrate(c, REPO_ROOT / "migrations")
        yield c


def ep(external_id="not_a", text="We agreed to ship on Friday.", **kw) -> Episode:
    body, sections = build_body([("transcript", text, False)])
    return Episode(
        source=kw.pop("source", "granola"),
        external_id=external_id,
        occurred_at=T0,
        title="Sync",
        body=body,
        sections=sections,
        participants=[Participant("creator", "Ada", "ada@example.com")],
        raw={"id": external_id},
        **kw,
    )


def rows(conn):
    return conn.execute(
        "SELECT id, source, external_id, content_hash, updated_at, deleted_at FROM episodes "
        "ORDER BY id"
    ).fetchall()


def test_insert_then_identical_is_noop(conn):
    assert store.upsert(conn, ep()) is Outcome.INSERTED
    before = rows(conn)
    assert store.upsert(conn, ep()) is Outcome.UNCHANGED
    assert rows(conn) == before, "an unchanged upsert must not write (updated_at included)"


def test_raw_only_change_is_noop(conn):
    store.upsert(conn, ep())
    before = rows(conn)
    assert store.upsert(conn, replace(ep(), raw={"id": "not_a", "etag": "v2"})) is Outcome.UNCHANGED
    assert rows(conn) == before


def test_changed_content_updates_in_place(conn):
    store.upsert(conn, ep())
    [(id1, _, _, h1, _, _)] = rows(conn)
    assert store.upsert(conn, ep(text="We agreed to ship on Monday.")) is Outcome.UPDATED
    after = rows(conn)
    assert len(after) == 1
    assert after[0][0] == id1, "same row, same id: provenance pointers stay valid"
    assert after[0][3] != h1


def test_content_change_resets_filter_status(conn):
    store.upsert(conn, ep())
    conn.execute("UPDATE episodes SET filter_status = 'kept'")
    store.upsert(conn, ep())  # unchanged: keeps its filter decision
    assert conn.execute("SELECT filter_status FROM episodes").fetchone()[0] == "kept"
    store.upsert(conn, ep(text="New text"))
    assert conn.execute("SELECT filter_status FROM episodes").fetchone()[0] == "pending"


def test_identical_content_different_ids_are_two_episodes(conn):
    """D4: content_hash is not identity. Two 'Thanks!' emails are two episodes."""
    assert store.upsert(conn, ep("not_a", text="Thanks!")) is Outcome.INSERTED
    assert store.upsert(conn, ep("not_b", text="Thanks!")) is Outcome.INSERTED
    r = rows(conn)
    assert len(r) == 2
    assert r[0][3] == r[1][3]


def test_same_external_id_different_sources_are_distinct(conn):
    store.upsert(conn, ep("x1", source="granola"))
    store.upsert(conn, ep("x1", source="gmail"))
    assert len(rows(conn)) == 2


def test_tombstone_is_idempotent_and_restore_works(conn):
    store.upsert(conn, ep())
    assert store.tombstone(conn, "granola", "not_a") is True
    assert store.tombstone(conn, "granola", "not_a") is False
    assert rows(conn)[0][5] is not None
    assert conn.execute("SELECT count(*) FROM live_episodes").fetchone()[0] == 0
    assert store.upsert(conn, ep()) is Outcome.RESTORED
    assert rows(conn)[0][5] is None
    assert conn.execute("SELECT count(*) FROM live_episodes").fetchone()[0] == 1


def test_tombstoned_then_changed_counts_as_update_and_restores(conn):
    store.upsert(conn, ep())
    store.tombstone(conn, "granola", "not_a")
    assert store.upsert(conn, ep(text="edited after restore")) is Outcome.UPDATED
    assert rows(conn)[0][5] is None


def test_tombstone_unknown_id_is_noop(conn):
    assert store.tombstone(conn, "granola", "never_seen") is False


def test_known_ids_reports_tombstone_state(conn):
    store.upsert(conn, ep("a"))
    store.upsert(conn, ep("b"))
    store.tombstone(conn, "granola", "b")
    assert store.known_ids(conn, "granola") == {"a": False, "b": True}
    assert store.known_ids(conn, "gmail") == {}


def test_purge_eligible_counts_only_old_tombstones(conn):
    store.upsert(conn, ep("a"))
    store.upsert(conn, ep("b"))
    store.tombstone(conn, "granola", "a")
    store.tombstone(conn, "granola", "b")
    conn.execute(
        "UPDATE episodes SET deleted_at = now() - interval '31 days' WHERE external_id='a'"
    )
    assert store.purge_eligible(conn, "granola", 30) == 1
    assert conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == 2, (
        "counting deletes nothing"
    )


def test_watermark_roundtrip(conn):
    assert store.get_watermark(conn, "granola") == {}
    store.set_watermark(conn, "granola", {"max_updated_at": "2026-03-02T15:00:00Z"})
    store.set_watermark(conn, "granola", {"max_updated_at": "2026-03-03T15:00:00Z"})
    assert store.get_watermark(conn, "granola") == {"max_updated_at": "2026-03-03T15:00:00Z"}


def test_db_rejects_bad_rows(conn):
    """Constraints are a second line of defence behind normalize()."""
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO episodes (source, external_id, occurred_at, raw, content_hash) "
            "VALUES ('dropbox', 'x', now(), '{}', repeat('a', 64))"
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO episodes (source, external_id, occurred_at, raw, content_hash) "
            "VALUES ('granola', 'x', now(), '{}', 'not-a-hash')"
        )


def test_content_change_clears_old_chunks_in_same_transaction(conn):
    store.upsert(conn, ep())
    eid, h = conn.execute("SELECT id, content_hash FROM episodes").fetchone()
    conn.execute(
        "INSERT INTO episode_chunks (episode_id, content_hash, kind, chunk_index, char_start,"
        " char_end, text) VALUES (%s, %s, 'body', 0, 0, 5, 'We ag')",
        (eid, h),
    )
    store.upsert(conn, ep())  # unchanged: chunks survive
    assert conn.execute("SELECT count(*) FROM episode_chunks").fetchone()[0] == 1
    store.upsert(conn, ep(text="Completely different text."))
    assert conn.execute("SELECT count(*) FROM episode_chunks").fetchone()[0] == 0


def test_hard_delete_cascades_to_chunks(conn):
    store.upsert(conn, ep())
    eid, h = conn.execute("SELECT id, content_hash FROM episodes").fetchone()
    conn.execute(
        "INSERT INTO episode_chunks (episode_id, content_hash, kind, chunk_index, text)"
        " VALUES (%s, %s, 'meta', 0, 'Sync')",
        (eid, h),
    )
    conn.execute("DELETE FROM episodes")
    assert conn.execute("SELECT count(*) FROM episode_chunks").fetchone()[0] == 0


def test_live_chunks_hides_tombstoned_and_stale(conn):
    store.upsert(conn, ep("a"))
    store.upsert(conn, ep("b"))
    for eid, h in conn.execute("SELECT id, content_hash FROM episodes").fetchall():
        conn.execute(
            "INSERT INTO episode_chunks (episode_id, content_hash, kind, chunk_index, text)"
            " VALUES (%s, %s, 'meta', 0, 'x'), (%s, %s, 'meta', 1, 'stale')",
            (eid, h, eid, "0" * 64),
        )
    store.tombstone(conn, "granola", "b")
    rows = conn.execute("SELECT text FROM live_chunks").fetchall()
    assert rows == [("x",)], "only live episodes, only current-hash chunks"
