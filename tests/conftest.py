import logging
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from pkos.config import Settings, get_settings


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in Path(str(item.fspath)).parts:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def base_settings() -> Settings:
    return Settings()


def _admin(settings: Settings) -> psycopg.Connection:
    return psycopg.connect(**settings.conninfo("postgres"), autocommit=True, connect_timeout=5)


@pytest.fixture
def fresh_db(base_settings: Settings):
    """A brand-new empty database per test, dropped afterwards. Returns Settings pointing at it."""
    name = f"pkos_test_{uuid.uuid4().hex[:12]}"
    try:
        admin = _admin(base_settings)
    except psycopg.OperationalError:
        pytest.fail(f"Postgres unreachable at {base_settings.db_host}:{base_settings.db_port}")
    with admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield base_settings.model_copy(update={"db_name": name})
    with _admin(base_settings) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest.fixture
def restore_logging():
    """Tests that call setup_logging must not leak handlers or the excepthook."""
    root = logging.getLogger()
    handlers, level, hook = root.handlers[:], root.level, sys.excepthook
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    sys.excepthook = hook
    get_settings.cache_clear()
