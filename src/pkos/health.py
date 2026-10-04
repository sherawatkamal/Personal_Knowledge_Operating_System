"""`pkos health`: is the install working? No secrets in the output."""

from dataclasses import dataclass

import psycopg

from pkos import db, migrate
from pkos.config import Settings


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_checks(settings: Settings) -> list[Check]:
    try:
        conn = db.connect(settings, autocommit=True)
    except psycopg.OperationalError as e:
        return [
            Check(
                "database",
                False,
                f"unreachable at {settings.db_host}:{settings.db_port} ({type(e).__name__})",
            )
        ]
    with conn:
        version = conn.execute("SHOW server_version").fetchone()[0]
        checks = [
            Check(
                "database", True, f"postgres {version} at {settings.db_host}, db {settings.db_name}"
            )
        ]

        row = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        checks.append(
            Check(
                "pgvector",
                row is not None,
                f"vector {row[0]}" if row else "extension not installed",
            )
        )

        try:
            st = migrate.status(conn, settings.migrations_dir)
        except migrate.MigrationError as e:
            checks.append(Check("migrations", False, str(e)))
        else:
            problems = []
            if st.pending:
                problems.append("pending: " + ", ".join(m.version for m in st.pending))
            if st.modified:
                problems.append("changed on disk: " + ", ".join(st.modified))
            if st.missing:
                problems.append("missing on disk: " + ", ".join(st.missing))
            detail = "; ".join(problems) or f"{len(st.applied)} applied, up to date"
            checks.append(Check("migrations", st.ok, detail))
    return checks
