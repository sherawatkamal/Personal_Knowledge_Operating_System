"""Single entry point: `pkos <command>`."""

import logging

import typer

from pkos import db, health, logs, migrate, sync
from pkos.config import get_settings

# Rich tracebacks print local variables, which can include credentials. Disabled:
# uncaught exceptions go through the scrubbing excepthook installed by setup_logging.
JUDGE_PROFILE = "groq-qwen-judge"  # a different model family from the answerer (M1)

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


def _embedder(settings):
    from pkos.embed.fast import FastEmbedder

    return FastEmbedder(settings.data_dir / "models")


def _index(conn, settings) -> None:
    from pkos.embed.index import index_pending

    r = index_pending(conn, _embedder(settings))
    _echo(
        f"indexed {r.episodes} episode(s), {r.chunks} chunk(s)"
        if r.episodes
        else "index up to date"
    )


@app.command("embed")
def embed_cmd() -> None:
    """Chunk and embed episodes whose current content isn't indexed yet (local model)."""
    settings = get_settings()
    with db.connect(settings, autocommit=True) as conn:
        _index(conn, settings)


@app.command("sync")
def sync_cmd(
    source: str = typer.Argument(None, help="Sync only this source."),
    embed: bool = typer.Option(True, help="Index new and changed episodes after syncing."),
) -> None:
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
        if embed:
            _index(conn, settings)
    if failed:
        raise typer.Exit(1)


@app.command("eval")
def eval_cmd(
    config: list[str] = typer.Option([], "--config", "-c", help="Config name; repeatable."),
    all_configs: bool = typer.Option(False, "--all", help="Every config in configs/systems."),
    split: str = typer.Option("dev", help="dev (tuning) or test (frozen, reported)."),
    questions: str = typer.Option(None, help="Question CSV (default: data/questions.csv)."),
    do_freeze: bool = typer.Option(False, "--freeze", help="Freeze the question set and exit."),
    markdown: bool = typer.Option(False, help="Print the publishable markdown tables."),
) -> None:
    """Score configurations against the question set and print the comparison table."""
    from pathlib import Path

    from pkos.eval import configs as cfg
    from pkos.eval import questions as qmod
    from pkos.eval import report, runner, scoring

    settings = get_settings()
    path = Path(questions) if questions else settings.data_dir / "questions.csv"
    if split not in qmod.SPLITS:
        log.error("--split must be one of %s", ", ".join(qmod.SPLITS))
        raise typer.Exit(2)
    try:
        qset = qmod.load(path)
    except (OSError, qmod.QuestionSetError) as e:
        log.error("%s", e)
        raise typer.Exit(1) from None
    if do_freeze:
        _echo(f"froze {path.name} ({qset.sha256[:12]}) -> {runner.freeze(qset, settings.data_dir)}")
        return
    try:
        names = list(cfg.available()) if all_configs else config
        if not names:
            log.error("give --config NAME (repeatable) or --all")
            raise typer.Exit(2)
        chosen = cfg.resolve(names)
    except cfg.ConfigError as e:
        log.error("%s", e)
        raise typer.Exit(1) from None

    from pkos import llm
    from pkos.eval import judge_model

    needs_models = any(c.kind == "retrieval" for c in chosen)
    embedder = _embedder(settings) if needs_models else None
    judge = scoring.Judge(
        cache=scoring.JudgeCache(settings.data_dir / "eval" / "judge_cache.jsonl")
    )
    with db.connect(settings, autocommit=True) as conn:
        if needs_models and settings.groq_api_key:
            judge_llm = llm.get_llm(JUDGE_PROFILE)
            judge.model_judge = judge_model.make_model_judge(conn, embedder, judge_llm)
            judge.model_id, judge.prompt_sha = judge_llm.profile.model, judge_model.PROMPT_SHA
        try:
            run = runner.run_eval(
                conn,
                qset,
                chosen,
                split,
                judge,
                settings.data_dir,
                embedder=embedder,
                llm_factory=llm.get_llm,
            )
        except llm.DailyLimitReached as e:
            log.error("%s", e)
            raise typer.Exit(3) from None
    text = report.render_text(run)
    out = runner.write_results(
        run, settings.data_dir, {"tables.txt": text, "tables.csv": report.render_csv(run)}
    )
    if markdown:
        try:
            _echo(report.render_markdown(run))
        except report.NotPublishable as e:
            log.error("%s", e)
            raise typer.Exit(1) from None
    else:
        _echo(text)
    _echo(llm.usage.summary())
    _echo(f"judge model calls: {judge.model_calls} (cache hits are free)   results: {out}")


@app.command("ask")
def ask_cmd(
    question: str = typer.Argument(..., help="One standalone question."),
    config: str = typer.Option("hybrid", "--config", "-c", help="System configuration."),
) -> None:
    """Answer one question from your archive, with numbered citations (same pipeline as eval)."""
    from pkos import llm
    from pkos.answer.pipeline import ask
    from pkos.eval import configs as cfg

    settings = get_settings()
    try:
        conf = cfg.resolve([config])[0]
    except cfg.ConfigError as e:
        log.error("%s", e)
        raise typer.Exit(1) from None
    if conf.kind != "retrieval":
        log.error("%s is a %s config; ask needs a retrieval config", conf.name, conf.kind)
        raise typer.Exit(1)
    modes = conf.raw.get("retrieval", {}).get("modes", [])
    embedder = _embedder(settings) if "vector" in modes else None
    with db.connect(settings, autocommit=True) as conn:
        try:
            res = ask(conn, conf.raw, question, embedder=embedder, llm_factory=llm.get_llm)
        except llm.LLMError as e:
            log.error("%s", e)
            raise typer.Exit(1) from None
    if res.abstained:
        _echo("Your archive doesn't support an answer to this.")
        for n in res.notes:
            _echo(f"  ({n})")
    else:
        for c in res.claims:
            _echo(f"- {c.text} " + "".join(f"[{n}]" for n in c.cites))
        _echo("")
        for n in sorted({n for c in res.claims for n in c.cites}):
            h = res.shown[n - 1]
            where = f"chars {h.span[0]}-{h.span[1]}" if h.span else "metadata"
            _echo(
                f"[{n}] {h.source} · {h.title or '(untitled)'} · {h.occurred_at:%Y-%m-%d} · "
                f"episode {h.episode_id} · {h.section or 'meta'} {where}"
            )
    _echo(
        f"\nconfig {conf.name} · retrieved {len(res.retrieved)} · shown {len(res.shown)} · "
        f"cited {len(res.cited_hits)}"
    )
    _echo(llm.usage.summary())


@app.command("find")
def find_cmd(
    text: str = typer.Argument(..., help="Words to look for."),
    limit: int = typer.Option(10, help="Episodes to list."),
) -> None:
    """Model-free lookup of episode ids, for writing gold sources."""
    from pkos.retrieve.chunks import fts

    settings = get_settings()
    with db.connect(settings, autocommit=True) as conn:
        best: dict[int, object] = {}
        for h in fts(conn, text, 200):
            best.setdefault(h.episode_id, h)
    for h in list(best.values())[:limit]:
        _echo(
            f"{h.episode_id:>6}  {h.source}:{h.external_id}  {h.occurred_at:%Y-%m-%d}  "
            f"{h.title or '(untitled)'}"
        )
    if not best:
        _echo("no matches")


@app.command("health")
def health_cmd() -> None:
    """Check the database, extensions and migrations."""
    settings = get_settings()
    checks = health.run_checks(settings)
    if all(c.ok for c in checks):
        checks += health.provider_checks(settings)
    width = max(len(c.name) for c in checks)
    for c in checks:
        status = "WARN" if c.warn else ("ok  " if c.ok else "FAIL")
        _echo(f"{status}  {c.name.ljust(width)}  {c.detail}")
    if not all(c.ok for c in checks):
        raise typer.Exit(1)
