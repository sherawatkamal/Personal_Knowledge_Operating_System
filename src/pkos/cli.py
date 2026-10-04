"""Single entry point: `pkos <command>`."""

import logging

import typer

from pkos import db, health, logs, migrate
from pkos.config import get_settings

# Rich tracebacks print local variables, which can include credentials. Disabled:
# uncaught exceptions go through the scrubbing excepthook installed by setup_logging.
app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False, add_completion=False)
log = logging.getLogger("pkos")


def _echo(text: str) -> None:
    """All CLI output is scrubbed too, not only log lines."""
    typer.echo(logs.scrub(text))


@app.callback()
def main() -> None:
    logs.setup_logging(get_settings().log_level)


@app.command("migrate")
def migrate_cmd() -> None:
    """Apply pending database migrations."""
    settings = get_settings()
    with db.connect(settings, autocommit=True) as conn:
        try:
            applied = migrate.migrate(conn, settings.migrations_dir)
        except migrate.MigrationError as e:
            log.error("%s", e)
            raise typer.Exit(1) from None
    if applied:
        for version in applied:
            _echo(f"applied {version}")
    else:
        _echo("migrations up to date")


@app.command("health")
def health_cmd() -> None:
    """Check the database, extensions and migrations."""
    checks = health.run_checks(get_settings())
    width = max(len(c.name) for c in checks)
    for c in checks:
        _echo(f"{'ok  ' if c.ok else 'FAIL'}  {c.name.ljust(width)}  {c.detail}")
    if not all(c.ok for c in checks):
        raise typer.Exit(1)
