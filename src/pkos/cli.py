"""Single entry point: `pkos <command>`."""

import logging

import typer

from pkos import db, health, logs, migrate, sync
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


def _connectors(settings, only: str | None):
    from pkos.connectors.granola import GranolaClient, GranolaConnector

    available = {}
    if settings.granola_api_key:
        available["granola"] = lambda: GranolaConnector(
            GranolaClient(settings.granola_api_key.get_secret_value())
        )
    if only:
        if only not in available:
            log.error(
                "source %r is not configured (configured: %s)", only, ", ".join(available) or "none"
            )
            raise typer.Exit(1)
        return {only: available[only]}
    return available


@app.command("sync")
def sync_cmd(source: str = typer.Argument(None, help="Sync only this source.")) -> None:
    """Pull new and changed items from every configured source. Never deletes data."""
    settings = get_settings()
    connectors = _connectors(settings, source)
    if not connectors:
        _echo("no sources configured: set PKOS_GRANOLA_API_KEY in .env")
        raise typer.Exit(1)
    failed = False
    with db.connect(settings, autocommit=True) as conn:
        for name, make in connectors.items():
            try:
                r = sync.sync_source(conn, make(), settings.purge_after_days)
            except Exception as e:  # one source failing must not hide the others' results
                log.error("%s: sync failed: %s", name, e)
                failed = True
                continue
            c = r.counts
            _echo(
                f"{name}: {c['inserted']} new, {c['updated']} updated, "
                f"{c['restored']} restored, {c['tombstoned']} deleted upstream, "
                f"{c['unchanged']} unchanged"
            )
            if r.purge_eligible:
                _echo(
                    f"{name}: {r.purge_eligible} deleted item(s) older than "
                    f"{settings.purge_after_days} days are eligible for `pkos purge`"
                )
    if failed:
        raise typer.Exit(1)


@app.command("health")
def health_cmd() -> None:
    """Check the database, extensions and migrations."""
    checks = health.run_checks(get_settings())
    width = max(len(c.name) for c in checks)
    for c in checks:
        _echo(f"{'ok  ' if c.ok else 'FAIL'}  {c.name.ljust(width)}  {c.detail}")
    if not all(c.ok for c in checks):
        raise typer.Exit(1)
