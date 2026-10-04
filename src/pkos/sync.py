"""Sync engine (careful code: see tests/integration/test_sync_engine.py).

Each change commits in its own transaction, so progress survives a crash. The watermark is
written only after every change was applied, so a crash means the next run re-fetches and
the idempotent upsert turns the repeats into no-ops. Nothing is ever lost or duplicated.
"""

from dataclasses import dataclass, field

import psycopg

from pkos.connectors.base import Connector
from pkos.episodes import store
from pkos.episodes.store import Outcome


@dataclass
class SyncResult:
    source: str
    counts: dict[str, int] = field(
        default_factory=lambda: {o.value: 0 for o in Outcome} | {"tombstoned": 0}
    )
    purge_eligible: int = 0

    @property
    def writes(self) -> int:
        return sum(v for k, v in self.counts.items() if k != Outcome.UNCHANGED.value)


def sync_source(
    conn: psycopg.Connection, connector: Connector, purge_after_days: int
) -> SyncResult:
    if not conn.autocommit:
        raise ValueError("sync_source needs an autocommit connection; it manages transactions")
    source = connector.source
    result = SyncResult(source)
    # One sync per source at a time: a second runner waits rather than interleaving.
    conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (f"pkos-sync:{source}",))
    try:
        watermark = store.get_watermark(conn, source)
        known = store.known_ids(conn, source)
        for change in connector.changes(watermark, known):
            with conn.transaction():
                if change.episode is None:
                    if store.tombstone(conn, source, change.external_id):
                        result.counts["tombstoned"] += 1
                else:
                    if change.episode.source != source:
                        raise ValueError(
                            f"{source} connector yielded a {change.episode.source} episode"
                        )
                    result.counts[store.upsert(conn, change.episode).value] += 1
        with conn.transaction():
            store.set_watermark(conn, source, connector.new_watermark())
        result.purge_eligible = store.purge_eligible(conn, source, purge_after_days)
        return result
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"pkos-sync:{source}",))
