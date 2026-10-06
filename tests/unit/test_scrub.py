"""A credential must never reach log output, tracebacks or CLI output."""

import logging
import sys

import pytest

from pkos import logs
from pkos.config import Settings, get_settings

PLANTED = "planted-Secret-Value-9f3k2"


@pytest.fixture
def planted_env(monkeypatch, restore_logging):
    monkeypatch.setenv("PKOS_DB_PASSWORD", PLANTED)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_settings_repr_hides_secret(planted_env):
    s = Settings()
    assert PLANTED not in repr(s)
    assert PLANTED not in str(s)
    assert PLANTED not in s.model_dump_json()


def test_loading_settings_registers_secret(planted_env):
    Settings()
    assert logs.scrub(f"oops {PLANTED} leaked") == f"oops {logs.REDACTED} leaked"


def test_log_message_and_args_scrubbed(planted_env, capsys):
    s = Settings()
    logs.setup_logging("DEBUG")
    log = logging.getLogger("pkos.test")
    log.debug("conninfo=%s", s.conninfo())  # the classic accidental debug line
    log.info(f"inline {PLANTED}")
    err = capsys.readouterr().err
    assert PLANTED not in err
    assert logs.REDACTED in err


def test_exception_traceback_scrubbed(planted_env, capsys):
    Settings()
    logs.setup_logging("INFO")
    try:
        raise RuntimeError(f"auth failed with key {PLANTED}")
    except RuntimeError:
        logging.getLogger("pkos.test").exception("request failed")
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert PLANTED not in err


def test_uncaught_exception_hook_scrubbed(planted_env, capsys):
    Settings()
    logs.setup_logging("INFO")
    try:
        raise ValueError(PLANTED)
    except ValueError:
        sys.excepthook(*sys.exc_info())
    err = capsys.readouterr().err
    assert "Unhandled exception" in err
    assert PLANTED not in err


@pytest.mark.parametrize(
    "leak",
    [
        "Authorization: Bearer abcdefghijklmnop1234",
        "key sk-ant-api03-abcdefghijklmnopqrstuv",
        "token xoxb-1234567890-abcdefghij",
        "access ya29.a0AfH6SMBabcdefghijklmnopqrstu",
        "refresh 1//0gabcdefghijklmnopqrstuvwxyz",
        "client GOCSPX-abcdefghijklmnop",
        "granola grn_abcdefghijklmnop0123",
        "groq gsk_abcdefghijklmnop0123",
        "password=hunter2hunter2",
        '{"api_key": "abc123def456"}',
        "refresh_token: zzzzzzzzzzzz",
    ],
)
def test_unregistered_credential_shapes_scrubbed(leak):
    out = logs.scrub(leak)
    secret_part = leak.split()[-1].strip('"}').split("=")[-1].strip('"')
    assert secret_part not in out, out
    assert logs.REDACTED in out


def test_short_values_not_registered():
    logs.register_secret("pkos")
    assert logs.scrub("pkos health") == "pkos health"
