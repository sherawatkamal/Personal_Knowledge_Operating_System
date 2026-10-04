"""Migration runner: idempotent, refuses edits, atomic per file, safe under concurrency."""

import threading
from pathlib import Path

import psycopg
import pytest

from pkos import db, migrate
from pkos.config import REPO_ROOT, Settings


def write(directory: Path, name: str, body: str) -> None:
    (directory / name).write_text(body)


def table_exists(conn: psycopg.Connection, name: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()[0]


@pytest.fixture
def conn(fresh_db: Settings):
    with db.connect(fresh_db, autocommit=True) as c:
        yield c


def test_repo_migrations_apply(conn):
    applied = migrate.migrate(conn, REPO_ROOT / "migrations")
    assert applied[0] == "0001_base"
    ext = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
    assert ext is not None


def test_migrate_twice_is_noop(conn, tmp_path):
    write(tmp_path, "0001_a.sql", "CREATE TABLE a (id int);")
    write(tmp_path, "0002_b.sql", "CREATE TABLE b (id int);")
    assert migrate.migrate(conn, tmp_path) == ["0001_a", "0002_b"]
    before = conn.execute(
        "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert migrate.migrate(conn, tmp_path) == []
    after = conn.execute(
        "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert before == after
    assert migrate.status(conn, tmp_path).ok


def test_edited_applied_migration_is_refused(conn, tmp_path):
    write(tmp_path, "0001_a.sql", "CREATE TABLE a (id int);")
    migrate.migrate(conn, tmp_path)
    write(tmp_path, "0001_a.sql", "CREATE TABLE a (id int, extra text);")
    write(tmp_path, "0002_b.sql", "CREATE TABLE b (id int);")
    with pytest.raises(migrate.MigrationError, match="0001_a"):
        migrate.migrate(conn, tmp_path)
    assert not table_exists(conn, "b"), "nothing may apply while an applied file is modified"
    assert migrate.status(conn, tmp_path).modified == ["0001_a"]


def test_failed_migration_rolls_back_completely(conn, tmp_path):
    write(tmp_path, "0001_a.sql", "CREATE TABLE a (id int);")
    write(tmp_path, "0002_b.sql", "CREATE TABLE b (id int);\nSELECT 1/0;")
    with pytest.raises(migrate.MigrationError, match="0002_b"):
        migrate.migrate(conn, tmp_path)
    assert table_exists(conn, "a")
    assert not table_exists(conn, "b"), "partial effects of a failed file must roll back"
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == ["0001_a"]

    write(tmp_path, "0002_b.sql", "CREATE TABLE b (id int);")  # fixed, never applied: allowed
    assert migrate.migrate(conn, tmp_path) == ["0002_b"]


def test_applied_migration_missing_on_disk_is_refused(conn, tmp_path):
    write(tmp_path, "0001_a.sql", "CREATE TABLE a (id int);")
    migrate.migrate(conn, tmp_path)
    (tmp_path / "0001_a.sql").unlink()
    with pytest.raises(migrate.MigrationError, match="missing"):
        migrate.migrate(conn, tmp_path)


@pytest.mark.parametrize("name", ["1_a.sql", "0001-a.sql", "0001_A.sql", "0001_a.SQL.sql"])
def test_bad_filenames_rejected(tmp_path, name):
    write(tmp_path, name, "SELECT 1;")
    with pytest.raises(migrate.MigrationError, match="filename"):
        migrate.discover(tmp_path)


def test_duplicate_numbers_rejected(tmp_path):
    write(tmp_path, "0001_a.sql", "SELECT 1;")
    write(tmp_path, "0001_b.sql", "SELECT 1;")
    with pytest.raises(migrate.MigrationError, match="duplicate"):
        migrate.discover(tmp_path)


def test_requires_autocommit_connection(fresh_db, tmp_path):
    with db.connect(fresh_db) as c, pytest.raises(migrate.MigrationError, match="autocommit"):
        migrate.migrate(c, tmp_path)


def test_concurrent_runners_apply_each_migration_once(fresh_db, tmp_path):
    # pg_sleep widens the race window; without the advisory lock both runners would try 0001.
    write(tmp_path, "0001_a.sql", "SELECT pg_sleep(0.5);\nCREATE TABLE a (id int);")
    write(tmp_path, "0002_b.sql", "CREATE TABLE b (id int);")
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            with db.connect(fresh_db, autocommit=True) as c:
                results.append(migrate.migrate(c, tmp_path))
        except BaseException as e:  # noqa: BLE001 - surfaced in the assertion below
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert sorted(results, key=len) == [[], ["0001_a", "0002_b"]]
