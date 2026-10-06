"""Writes to the episode store (careful code: see tests/integration/test_episode_store.py).

Every write here is idempotent: applying the same episode twice is a no-op the second time.
"""

from dataclasses import asdict
from enum import StrEnum
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from pkos.episodes.model import Episode
from pkos.episodes.normalize import content_hash, normalize


class Outcome(StrEnum):
    INSERTED = "inserted"
    UPDATED = "updated"  # content changed
    RESTORED = "restored"  # was tombstoned, came back unchanged
    UNCHANGED = "unchanged"


# The CTE reads the row as it was before this statement, so we can tell what happened.
# The WHERE on DO UPDATE makes an unchanged, live episode a true no-op: no write, no new
# updated_at, nothing for later steps to re-derive.
_UPSERT = """
WITH old AS (
    SELECT content_hash, deleted_at FROM episodes
    WHERE source = %(source)s AND external_id = %(external_id)s
)
INSERT INTO episodes (source, external_id, thread_id, occurred_at, ends_at, title,
                      participants, refs, meta, body, sections, raw, content_hash)
VALUES (%(source)s, %(external_id)s, %(thread_id)s, %(occurred_at)s, %(ends_at)s, %(title)s,
        %(participants)s, %(refs)s, %(meta)s, %(body)s, %(sections)s, %(raw)s, %(content_hash)s)
ON CONFLICT (source, external_id) DO UPDATE SET
    thread_id = EXCLUDED.thread_id, occurred_at = EXCLUDED.occurred_at,
    ends_at = EXCLUDED.ends_at, title = EXCLUDED.title, participants = EXCLUDED.participants,
    refs = EXCLUDED.refs, meta = EXCLUDED.meta, body = EXCLUDED.body,
    sections = EXCLUDED.sections, raw = EXCLUDED.raw, content_hash = EXCLUDED.content_hash,
    filter_status = CASE WHEN episodes.content_hash = EXCLUDED.content_hash
                         THEN episodes.filter_status ELSE 'pending' END,
    filter_reason = CASE WHEN episodes.content_hash = EXCLUDED.content_hash
                         THEN episodes.filter_reason ELSE NULL END,
    deleted_at = NULL,
    updated_at = now()
WHERE episodes.content_hash <> EXCLUDED.content_hash OR episodes.deleted_at IS NOT NULL
RETURNING id,
    (SELECT content_hash FROM old) AS old_hash,
    (SELECT deleted_at FROM old) AS old_deleted
"""


def upsert(conn: psycopg.Connection, episode: Episode) -> Outcome:
    ep = normalize(episode)
    h = content_hash(ep)
    params: dict[str, Any] = {
        "source": ep.source,
        "external_id": ep.external_id,
        "thread_id": ep.thread_id,
        "occurred_at": ep.occurred_at,
        "ends_at": ep.ends_at,
        "title": ep.title,
        "participants": Jsonb([_drop_none(asdict(p)) for p in ep.participants]),
        "refs": Jsonb(ep.refs),
        "meta": Jsonb(ep.meta),
        "body": ep.body,
        "sections": Jsonb([asdict(s) for s in ep.sections]),
        "raw": Jsonb(ep.raw),
        "content_hash": h,
    }
    row = conn.execute(_UPSERT, params).fetchone()
    if row is None:
        return Outcome.UNCHANGED
    _, old_hash, _old_deleted = row
    if old_hash is None:
        return Outcome.INSERTED
    if old_hash != h:
        # Derived rows describe text that no longer exists: clear them in this transaction.
        # (facts and commitments join this list in step 8.)
        conn.execute(
            "DELETE FROM episode_chunks WHERE episode_id = %s AND content_hash <> %s", (row[0], h)
        )
        return Outcome.UPDATED
    return Outcome.RESTORED


def tombstone(conn: psycopg.Connection, source: str, external_id: str) -> bool:
    """Mark an upstream-deleted item. Idempotent; returns True only if this call tombstoned it."""
    cur = conn.execute(
        "UPDATE episodes SET deleted_at = now() "
        "WHERE source = %s AND external_id = %s AND deleted_at IS NULL",
        (source, external_id),
    )
    return cur.rowcount == 1


def known_ids(conn: psycopg.Connection, source: str) -> dict[str, bool]:
    """external_id -> is_tombstoned, for every stored episode of this source."""
    rows = conn.execute(
        "SELECT external_id, deleted_at IS NOT NULL FROM episodes WHERE source = %s", (source,)
    ).fetchall()
    return dict(rows)


def get_watermark(conn: psycopg.Connection, source: str) -> dict[str, Any]:
    row = conn.execute("SELECT watermark FROM sync_state WHERE source = %s", (source,)).fetchone()
    return row[0] if row else {}


def set_watermark(conn: psycopg.Connection, source: str, watermark: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (source, watermark, last_success_at, updated_at)
        VALUES (%s, %s, now(), now())
        ON CONFLICT (source) DO UPDATE SET
            watermark = EXCLUDED.watermark, last_success_at = now(), updated_at = now()
        """,
        (source, Jsonb(watermark)),
    )


def purge_eligible(conn: psycopg.Connection, source: str, after_days: int) -> int:
    """Tombstones old enough to purge. Reported by sync; nothing is deleted here (S1)."""
    return conn.execute(
        "SELECT count(*) FROM episodes WHERE source = %s AND deleted_at IS NOT NULL "
        "AND deleted_at < now() - make_interval(days => %s)",
        (source, after_days),
    ).fetchone()[0]


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}
