"""CLI output must never carry a credential, even on failure."""

from typer.testing import CliRunner

from pkos.config import get_settings

PLANTED = "planted-Secret-Value-7q1z"


def test_cli_health_failure_does_not_print_secret(monkeypatch, restore_logging):
    monkeypatch.setenv("PKOS_DB_PASSWORD", PLANTED)
    monkeypatch.setenv("PKOS_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("PKOS_DB_PORT", "1")  # nothing listens here: immediate refusal
    get_settings.cache_clear()
    from pkos.cli import app

    result = CliRunner().invoke(app, ["health"])
    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert PLANTED not in result.output
