"""Build chunks + embeddings for episodes whose current content has none. Zero work on re-run."""

from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row

from pkos.embed.base import Embedder
from pkos.embed.chunker import chunk_body, render_meta

_PENDING = """
SELECT e.id, e.source, e.title, e.occurred_at, e.ends_at, e.participants, e.meta, e.body,
       e.sections, e.content_hash
FROM live_episodes e
WHERE NOT EXISTS (SELECT 1 FROM episode_chunks c
                  WHERE c.episode_id = e.id AND c.content_hash = e.content_hash)
ORDER BY e.occurred_at DESC
"""


@dataclass
class IndexResult:
    episodes: int = 0
    chunks: int = 0


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def index_pending(conn: psycopg.Connection, embedder: Embedder) -> IndexResult:
    result = IndexResult()
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(_PENDING).fetchall()
    for row in rows:
        chunks = chunk_body(row["body"], row["sections"])
        title = row["title"] or ""
        texts = [render_meta(row)] + [row["body"][c.start : c.end] for c in chunks]
        # Embed body chunks with the title as context; stored text stays an exact body slice.
        inputs = [texts[0]] + [f"{title}\n{t}" if title else t for t in texts[1:]]
        vectors = embedder.embed_documents(inputs)
        with conn.transaction():
            conn.execute("DELETE FROM episode_chunks WHERE episode_id = %s", (row["id"],))
            params = [
                (
                    row["id"],
                    row["content_hash"],
                    "meta",
                    0,
                    None,
                    None,
                    None,
                    texts[0],
                    _vec(vectors[0]),
                    embedder.name,
                )
            ]
            for i, c in enumerate(chunks):
                params.append(
                    (
                        row["id"],
                        row["content_hash"],
                        "body",
                        i,
                        c.start,
                        c.end,
                        c.section,
                        texts[i + 1],
                        _vec(vectors[i + 1]),
                        embedder.name,
                    )
                )
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO episode_chunks (episode_id, content_hash, kind, chunk_index,"
                    " char_start, char_end, section, text, embedding, embedding_model)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
                    params,
                )
        result.episodes += 1
        result.chunks += len(params)
    return result
