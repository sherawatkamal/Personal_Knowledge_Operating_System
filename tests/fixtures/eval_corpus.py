"""A tiny synthetic corpus matching tests/fixtures/eval/synthetic_questions.csv. Fictional.

Granola and Gmail are 'connected'; Calendar is not. One Gmail episode is tombstoned and
q12 points at an id that doesn't exist, so every skip reason is exercised.

    PKOS_DB_NAME=pkos_evaldemo python -m tests.fixtures.eval_corpus
"""

from datetime import UTC, datetime

import psycopg

from pkos.episodes import Episode, Participant, store
from pkos.episodes.normalize import build_body

T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)

EPISODES = [
    (
        "granola",
        "syn_g1",
        "Budget review",
        "Pat: The Q3 budget is 40,000 dollars.\nAda: I'll send the revised deck by Friday.",
    ),
    ("granola", "syn_g2", "Hiring sync", "Pat: Jordan Lee joined the platform team in March."),
    (
        "gmail",
        "syn_m1",
        "Intro: Ada <> Sam",
        "Ada, meet Sam Rivera from Northwind. Sam, Ada leads the data project. -- Pat",
    ),
    ("gmail", "syn_m2", "Offsite logistics", "The offsite is in Lisbon on May 14."),
    ("gmail", "syn_m3", "Contract", "The contract renewal date is June 1."),
]


def seed(conn: psycopg.Connection) -> None:
    for source, ext, title, text in EPISODES:
        body, sections = build_body([("body", text, False)])
        store.upsert(
            conn,
            Episode(
                source,
                ext,
                T0,
                title=title,
                body=body,
                sections=sections,
                participants=[Participant("creator", "Ada Example", "ada@example.com")],
            ),
        )
    store.tombstone(conn, "gmail", "syn_m3")
    for source in ("granola", "gmail"):
        store.set_watermark(conn, source, {"synthetic": True})


if __name__ == "__main__":
    from pkos import db, migrate
    from pkos.config import get_settings

    settings = get_settings()
    with db.connect(settings, autocommit=True) as c:
        migrate.migrate(c, settings.migrations_dir)
        seed(c)
    print(f"seeded {len(EPISODES)} synthetic episodes into {settings.db_name}")
