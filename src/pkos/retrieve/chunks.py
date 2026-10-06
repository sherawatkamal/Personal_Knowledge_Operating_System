"""Full text, vector, and reciprocal-rank fusion over live_chunks (the step 4 baseline)."""

from dataclasses import dataclass, field
from datetime import datetime

import psycopg

from pkos.embed.base import Embedder

MODES = ("fts", "vector")

_COLS = """c.id, c.episode_id, c.kind, c.char_start, c.char_end, c.section, c.text,
           e.source, e.external_id, e.title, e.occurred_at"""

# OR over the question's lexemes, ranked by cover density. websearch_to_tsquery would AND every
# term, and a natural-language question then almost never matches: an unfairly weak baseline.
_FTS = f"""
WITH q AS (
    SELECT to_tsquery('english', array_to_string(tsvector_to_array(to_tsvector('english', %s)),
                                                 ' | ')) AS tsq
)
SELECT {_COLS}, ts_rank_cd(c.tsv, q.tsq) AS score
FROM live_chunks c JOIN episodes e ON e.id = c.episode_id, q
WHERE q.tsq IS NOT NULL AND numnode(q.tsq) > 0 AND c.tsv @@ q.tsq
ORDER BY score DESC, c.id
LIMIT %s
"""

_VECTOR = f"""
SELECT {_COLS}, 1 - (c.embedding <=> %s::vector) AS score
FROM live_chunks c JOIN episodes e ON e.id = c.episode_id
WHERE c.embedding IS NOT NULL
ORDER BY c.embedding <=> %s::vector, c.id
LIMIT %s
"""


@dataclass
class Hit:
    chunk_id: int
    episode_id: int
    kind: str
    span: tuple[int, int] | None
    section: str | None
    text: str
    source: str
    external_id: str
    title: str | None
    occurred_at: datetime
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def fused(self) -> float:
        return self.scores.get("rrf", 0.0)


def _hits(rows, mode: str) -> list[Hit]:
    out = []
    for rank, (cid, eid, kind, s, e, sec, text, src, ext, title, at, score) in enumerate(rows, 1):
        out.append(
            Hit(
                cid,
                eid,
                kind,
                (s, e) if s is not None else None,
                sec,
                text,
                src,
                ext,
                title,
                at,
                {f"{mode}_rank": rank, f"{mode}_score": float(score)},
            )
        )
    return out


def fts(conn: psycopg.Connection, query: str, limit: int) -> list[Hit]:
    return _hits(conn.execute(_FTS, (query, limit)).fetchall(), "fts")


def vector(conn: psycopg.Connection, embedder: Embedder, query: str, limit: int) -> list[Hit]:
    v = "[" + ",".join(f"{x:.6f}" for x in embedder.embed_query(query)) + "]"
    return _hits(conn.execute(_VECTOR, (v, v, limit)).fetchall(), "vector")


def rrf(lists: list[list[Hit]], k: int = 60) -> list[Hit]:
    """Reciprocal rank fusion: score = sum over lists of 1 / (k + rank)."""
    merged: dict[int, Hit] = {}
    for hits in lists:
        for rank, h in enumerate(hits, 1):
            m = merged.setdefault(h.chunk_id, h)
            if m is not h:
                m.scores.update(h.scores)
            m.scores["rrf"] = m.scores.get("rrf", 0.0) + 1.0 / (k + rank)
    return sorted(merged.values(), key=lambda h: (-h.fused, h.chunk_id))


def search(
    conn: psycopg.Connection,
    embedder: Embedder | None,
    query: str,
    *,
    modes: list[str],
    pool: int = 50,
    rrf_k: int = 60,
) -> list[Hit]:
    unknown = set(modes) - set(MODES)
    if unknown or not modes:
        raise ValueError(f"modes must be a non-empty subset of {MODES}, got {modes}")
    lists = []
    if "fts" in modes:
        lists.append(fts(conn, query, pool))
    if "vector" in modes:
        if embedder is None:
            raise ValueError("vector mode needs an embedder")
        lists.append(vector(conn, embedder, query, pool))
    return rrf(lists, rrf_k)
