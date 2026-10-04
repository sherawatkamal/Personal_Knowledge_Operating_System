"""Granola connector through the real sync engine and database: the step 2 guarantees."""

import httpx
import pytest

from pkos import db, migrate
from pkos.config import REPO_ROOT
from pkos.connectors.granola import GranolaClient, GranolaConnector
from pkos.sync import sync_source
from tests.fixtures.granola import FakeGranola

KEY = "grn_syntheticTestKey0123456789abcdef"


@pytest.fixture
def conn(fresh_db):
    with db.connect(fresh_db, autocommit=True) as c:
        migrate.migrate(c, REPO_ROOT / "migrations")
        yield c


def run(conn, fake):
    client = GranolaClient(
        KEY, transport=httpx.MockTransport(fake.handler), min_interval=0, sleep=lambda s: None
    )
    return sync_source(conn, GranolaConnector(client), 30)


def test_sync_twice_no_duplicates_no_writes(conn):
    fake = FakeGranola(page_size=2)
    for i in range(5):
        fake.add(f"not_{i}", f"2026-03-0{i + 1}T10:00:00Z")
    assert run(conn, fake).counts["inserted"] == 5
    before = conn.execute("SELECT * FROM episodes ORDER BY id").fetchall()
    second = run(conn, fake)
    assert second.writes == 0
    assert conn.execute("SELECT * FROM episodes ORDER BY id").fetchall() == before


def test_edited_note_updates_in_place(conn):
    fake = FakeGranola()
    fake.add("not_1", "2026-03-01T10:00:00Z")
    run(conn, fake)
    fake.add("not_1", "2026-03-09T10:00:00Z", summary_text="Revised summary.")
    r = run(conn, fake)
    assert r.counts["updated"] == 1
    assert conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == 1


def test_deleted_note_is_tombstoned_then_restored(conn):
    fake = FakeGranola()
    fake.add("not_1", "2026-03-01T10:00:00Z")
    fake.add("not_2", "2026-03-01T11:00:00Z")
    run(conn, fake)
    gone = fake.notes.pop("not_2")
    assert run(conn, fake).counts["tombstoned"] == 1
    assert conn.execute("SELECT count(*) FROM live_episodes").fetchone()[0] == 1
    fake.notes["not_2"] = gone
    assert run(conn, fake).counts["restored"] == 1


def test_stored_body_matches_section_offsets(conn):
    fake = FakeGranola()
    fake.add("not_1", "2026-03-01T10:00:00Z", private_notes_text="Mine.")
    run(conn, fake)
    body, sections = conn.execute("SELECT body, sections FROM episodes").fetchone()
    assert [body[s["start"] : s["end"]].split("\n")[0] for s in sections] == [
        "Mine.",
        "Test Owner: Morning.",
        "Budget review. Owner to send the deck by Friday.",
    ]
