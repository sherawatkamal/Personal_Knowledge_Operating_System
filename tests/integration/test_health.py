from pkos import db, health, migrate
from pkos.config import REPO_ROOT


def by_name(checks):
    return {c.name: c for c in checks}


def test_health_fails_before_migrations(fresh_db):
    checks = by_name(health.run_checks(fresh_db))
    assert checks["database"].ok
    assert not checks["pgvector"].ok
    assert not checks["migrations"].ok
    assert "pending: 0001_base" in checks["migrations"].detail


def test_health_ok_after_migrations(fresh_db):
    with db.connect(fresh_db, autocommit=True) as conn:
        migrate.migrate(conn, REPO_ROOT / "migrations")
    checks = health.run_checks(fresh_db)
    assert all(c.ok for c in checks), checks
    assert fresh_db.db_password.get_secret_value() not in repr(checks)
