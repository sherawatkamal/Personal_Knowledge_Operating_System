"""Plain-SQL migration runner.

Rules:
- Files are named NNNN_snake_case.sql and applied in numeric order.
- Each file runs in its own transaction together with its schema_migrations row,
  so a failing migration leaves no trace. (So CREATE INDEX CONCURRENTLY is not allowed.)
- An applied file is checksummed. Editing it afterwards is refused, never silently
  ignored: add a new migration instead.
- A session advisory lock serialises concurrent runners (e.g. `up` racing `run`).
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_LOCK_KEY = 0x706B6F73  # "pkos"

BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: str  # file stem, e.g. "0001_base"
    sql: str
    checksum: str


@dataclass(frozen=True)
class Status:
    applied: list[str]
    pending: list[Migration]
    modified: list[str]  # applied, but the file on disk no longer matches
    missing: list[str]  # applied, but no file on disk

    @property
    def ok(self) -> bool:
        return not (self.pending or self.modified or self.missing)


def discover(directory: Path) -> list[Migration]:
    migrations: list[Migration] = []
    numbers: dict[str, str] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.match(path.name)
        if not match:
            raise MigrationError(f"bad migration filename {path.name!r}; expected NNNN_name.sql")
        number = match.group(1)
        if number in numbers:
            raise MigrationError(
                f"duplicate migration number {number}: {numbers[number]}, {path.name}"
            )
        numbers[number] = path.name
        data = path.read_bytes()
        migrations.append(
            Migration(path.stem, data.decode("utf-8"), hashlib.sha256(data).hexdigest())
        )
    return migrations


def status(conn: psycopg.Connection, directory: Path) -> Status:
    on_disk = discover(directory)
    exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()[0]
    applied: dict[str, str] = {}
    if exists:
        applied = dict(conn.execute("SELECT version, checksum FROM schema_migrations").fetchall())
    disk = {m.version: m for m in on_disk}
    return Status(
        applied=sorted(applied),
        pending=[m for m in on_disk if m.version not in applied],
        modified=sorted(v for v, c in applied.items() if v in disk and disk[v].checksum != c),
        missing=sorted(v for v in applied if v not in disk),
    )


def migrate(conn: psycopg.Connection, directory: Path) -> list[str]:
    """Apply pending migrations. Returns the versions applied (empty when up to date)."""
    if not conn.autocommit:
        raise MigrationError("migrate() needs an autocommit connection; it manages transactions")
    conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
    try:
        conn.execute(BOOTSTRAP_SQL)
        st = status(conn, directory)
        if st.modified:
            raise MigrationError(
                f"applied migration(s) changed on disk: {', '.join(st.modified)}. "
                "Restore the original file and add a new migration instead."
            )
        if st.missing:
            raise MigrationError(f"applied migration(s) missing on disk: {', '.join(st.missing)}")
        done: list[str] = []
        for m in st.pending:
            try:
                with conn.transaction():
                    conn.execute(m.sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (m.version, m.checksum),
                    )
            except psycopg.Error as e:
                raise MigrationError(
                    f"migration {m.version} failed and was rolled back: {e}"
                ) from e
            done.append(m.version)
        return done
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
