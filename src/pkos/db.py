"""Database connections."""

import psycopg

from pkos.config import Settings, get_settings


def connect(settings: Settings | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    settings = settings or get_settings()
    return psycopg.connect(**settings.conninfo(), autocommit=autocommit, connect_timeout=5)
