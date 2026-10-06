from datetime import UTC, datetime

import pytest

from pkos import db, migrate
from pkos.config import REPO_ROOT
from pkos.embed.index import index_pending
from pkos.episodes import Episode, Participant, store
from pkos.episodes.normalize import build_body
from tests.fixtures.embedder import HashEmbedder

T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)


@pytest.fixture
def conn(fresh_db):
    with db.connect(fresh_db, autocommit=True) as c:
        migrate.migrate(c, REPO_ROOT / "migrations")
        yield c


def put(conn, ext, text):
    body, sections = build_body([("transcript", text, False)])
    store.upsert(
        conn,
        Episode(
            "granola",
            ext,
            T0,
            title="Budget review",
            body=body,
            sections=sections,
            participants=[Participant("creator", "Ada", "ada@example.com")],
        ),
    )


def test_index_is_incremental_and_exact(conn):
    put(conn, "a", "Pat: The budget is 40,000.\nAda: I'll send the deck.")
    put(conn, "b", "Short note.")
    r = index_pending(conn, HashEmbedder())
    assert r.episodes == 2 and r.chunks == 4  # one meta + one body chunk each
    assert index_pending(conn, HashEmbedder()).episodes == 0, "re-run does zero work"
    for body, start, end, text in conn.execute(
        "SELECT e.body, c.char_start, c.char_end, c.text FROM episode_chunks c "
        "JOIN episodes e ON e.id = c.episode_id WHERE c.kind = 'body'"
    ):
        assert body[start:end] == text
    meta = conn.execute("SELECT text FROM episode_chunks WHERE kind='meta' LIMIT 1").fetchone()[0]
    assert "Title: Budget review" in meta and "Ada <ada@example.com> (creator)" in meta


def test_changed_episode_is_reindexed_once(conn):
    put(conn, "a", "First version.")
    index_pending(conn, HashEmbedder())
    put(conn, "a", "Second version, longer text.")
    r = index_pending(conn, HashEmbedder())
    assert r.episodes == 1
    texts = [t for (t,) in conn.execute("SELECT text FROM live_chunks WHERE kind='body'")]
    assert texts == ["Second version, longer text."]


def test_tombstoned_episodes_are_not_indexed(conn):
    put(conn, "a", "Gone soon.")
    store.tombstone(conn, "granola", "a")
    assert index_pending(conn, HashEmbedder()).episodes == 0
