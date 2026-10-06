from datetime import UTC, datetime

import pytest

from pkos import db, migrate
from pkos.config import REPO_ROOT
from pkos.embed.index import index_pending
from pkos.episodes import Episode, store
from pkos.episodes.normalize import build_body
from pkos.retrieve import chunks
from tests.fixtures.embedder import HashEmbedder

T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
DOCS = {
    "budget": "Pat: The Q3 budget is forty thousand dollars for the platform team.",
    "deck": "Ada: I will send the revised deck by Friday after the review.",
    "lunch": "Sam: Lunch is at noon on Thursday near the office.",
}


@pytest.fixture
def conn(fresh_db):
    with db.connect(fresh_db, autocommit=True) as c:
        migrate.migrate(c, REPO_ROOT / "migrations")
        for ext, text in DOCS.items():
            body, secs = build_body([("transcript", text, False)])
            store.upsert(
                c, Episode("granola", ext, T0, title=ext.title(), body=body, sections=secs)
            )
        index_pending(c, HashEmbedder())
        yield c


def ext_ids(hits):
    return [h.external_id for h in hits if h.kind == "body"]


def test_fts_uses_or_semantics_for_natural_questions(conn):
    hits = chunks.fts(conn, "What is the budget for the platform team this quarter?", 10)
    assert ext_ids(hits)[0] == "budget", "a natural question must match without every word"


def test_fts_handles_stopword_only_queries(conn):
    assert chunks.fts(conn, "what is the", 10) == []


def test_vector_ranks_relevant_chunk_first(conn):
    hits = chunks.vector(conn, HashEmbedder(), "revised deck Friday", 10)
    assert ext_ids(hits)[0] == "deck"
    assert 0 < hits[0].scores["vector_score"] <= 1


def test_rrf_merges_scores_and_orders(conn):
    hits = chunks.search(conn, HashEmbedder(), "budget platform team", modes=["fts", "vector"])
    top = hits[0]
    assert top.external_id == "budget"
    assert {"fts_rank", "vector_rank", "rrf"} <= set(top.scores)
    assert [h.fused for h in hits] == sorted((h.fused for h in hits), reverse=True)


def test_spans_are_exact_body_offsets(conn):
    for h in chunks.search(conn, HashEmbedder(), "deck", modes=["fts", "vector"]):
        if h.kind == "body":
            body = conn.execute(
                "SELECT body FROM episodes WHERE id=%s", (h.episode_id,)
            ).fetchone()[0]
            assert body[h.span[0] : h.span[1]] == h.text


def test_tombstoned_episodes_never_retrieved(conn):
    store.tombstone(conn, "granola", "budget")
    hits = chunks.search(conn, HashEmbedder(), "budget platform", modes=["fts", "vector"])
    assert "budget" not in {h.external_id for h in hits}


def test_bad_modes_rejected(conn):
    with pytest.raises(ValueError):
        chunks.search(conn, None, "x", modes=["bm25"])
    with pytest.raises(ValueError, match="embedder"):
        chunks.search(conn, None, "x", modes=["vector"])
