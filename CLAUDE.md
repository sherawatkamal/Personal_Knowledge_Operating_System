# CLAUDE.md

Personal knowledge operating system (pkos): a self-hosted system that ingests a person's own email, calendar, meeting notes and chat into one Postgres store, extracts facts with provenance, and answers questions with citations back to the source item. Design: `PROPOSAL.md`. Current scope, build order and every closed decision: `PLAN.md` (phase one). Read PLAN.md §5 before changing anything structural.

## Stack

- Python 3.12, managed with uv (`pyproject.toml`, `uv.lock`). CLI with typer.
- Postgres 17.11 + pgvector 0.8.7 (`pgvector/pgvector:0.8.7-pg17`), psycopg 3. No ORM.
- Images are pinned (`python:3.12.15-slim-trixie`, `ghcr.io/astral-sh/uv:0.12.23`) and verified native on linux/arm64. Bump deliberately, not by tag drift.
- Docker Compose: `db` (Postgres, no published port), `migrate` (one-shot), `app` (prints health and exits, for now). `docker compose up` stays attached to the db; use `up -d` to detach.
- Later steps add: fastembed (local embeddings), a thin LLM layer with hosted and Ollama implementations, FastAPI for one page.

## Commands

| What | Command |
|---|---|
| Install and start | `docker compose up` (builds, starts db, runs migrations, prints health) |
| Any pkos command | `./pkos <command>` (runs in the app container; migrations run first) |
| Health check | `./pkos health` |
| Sync sources | `./pkos sync [granola]` (never deletes; reports purge-eligible tombstones) |
| Eval | `./pkos eval -c null -c oracle [--split dev\|test] [--questions PATH] [--markdown]`; `--all`; `--freeze` |
| Apply migrations | `./pkos migrate` (also runs automatically) |
| Tests | `./pkos test` (pytest in the container; extra args go to pytest) |
| Lint / format | `docker compose run --rm app ruff check src tests` / `ruff format` |

Tests create a fresh database per test (`pkos_test_<random>`) on the same server and drop it afterwards, so they never touch the `pkos` database.

## Layout

- `src/pkos/episodes/`: Episode model, `normalize.py` (canonical hash, `build_body`), `store.py` (idempotent upsert, tombstone, watermarks).
- `src/pkos/connectors/`: `base.py` (Connector, Change), `granola.py`. `src/pkos/sync.py`: the sync engine.
- `src/pkos/eval/`: `questions.py` (CSV + validation), `configs.py` (`configs/systems/*.toml`), `systems.py` (System interface, null, oracle), `scoring.py` (gold resolution and skip reasons, judge and cache), `runner.py` (freeze, test ledger, results), `report.py` (tables).
- `src/pkos/cli.py`: entry point. `config.py`: settings from `PKOS_*` env vars. `logs.py`: secret scrubbing. `db.py`: connections. `migrate.py`: migration runner. `health.py`: health checks.
- `migrations/NNNN_name.sql`: plain SQL, applied in order.
- `tests/unit` (no DB) and `tests/integration` (real Postgres, auto-marked `integration`).
- `data/` is gitignored and holds all personal data: questions, eval results, dumps, OAuth tokens.

## Schema (as built so far)

- `schema_migrations(version, checksum, applied_at)`: created by the runner itself.
- `0001_base`: `CREATE EXTENSION vector`.
- `0002_episodes`:
  - `sync_state(source, watermark jsonb, last_success_at)`
  - `episodes`: identity `UNIQUE (source, external_id)`; `content_hash` indexed, not unique; `body` holds source text only, and `sections` gives its exact offsets; `raw` is never hashed; `deleted_at` is the tombstone
  - view `live_episodes`

The full phase-one schema is in PLAN.md §2. Tables are added only by the step that needs them.

## Conventions

**Migrations**
- Never edit an applied migration. The runner checksums files and refuses to run if one changed. Add a new file.
- Each file runs in one transaction with its `schema_migrations` row. So no `CREATE INDEX CONCURRENTLY` and no other non-transactional statements.
- Concurrent runners are serialised by an advisory lock.

**Secrets**
- Credentials are `SecretStr` in `Settings`. Loading settings registers every secret value (8+ characters) with the scrubber.
- All log output, tracebacks and CLI output (`_echo`) pass through `logs.scrub`. It also redacts known credential shapes: bearer tokens, `sk-`, `xox?-`, `ya29.`, `1//`, `GOCSPX-`, and `key=value` pairs.
- Typer's rich tracebacks are disabled, because they print local variables.
- Never print `Settings.conninfo()` or a token. `.env` is gitignored, and `.env.example` holds placeholders only.

**Personal data**
- No real email, calendar, notes or chat ever goes in the repo, including test fixtures. Fixtures are synthetic.
- Question sets and eval results are personal data and live in `data/`.

**Careful code** (test first, deliberately): idempotent sync and content-hash dedup, the episode-to-fact provenance chain, and anything that deletes. Connectors, scripts and the interface can move fast.

**Connectors**
- Yield `Change(external_id, episode | None)`. `None` means deleted upstream, and the engine tombstones it.
- Build `body` with `build_body()` so section offsets are exact, and mark model-written sections `generated`.
- Put stable fields in `meta` (it's hashed) and anything volatile only in `raw`.
- Test against synthetic fakes in `tests/fixtures/`, never recorded real responses.

**Sync**
- One transaction per change. The watermark is written last, so a crash leads to a re-fetch, and the upsert turns the repeats into no-ops.

**Eval**
- A configuration is a file; never add a code path for an ablation.
- Tune on `dev` only. `test` runs are recorded in the ledger, and a changed config gets a †.
- Diagnostic configs (oracle) never reach published (markdown) tables.
- The synthetic question set and corpus live in `tests/fixtures/` and are generated in code. Your real `questions.csv`, results, ledger and freeze files live in `data/eval/`.

**Model calls** go through the `llm/` abstraction only, never a provider SDK from business logic. Every extraction is cached by a hash of its exact input, so re-running on unchanged data must make zero model calls.

**Commits**: small, one logical change each. Update the PLAN.md checklist as items complete. Ask rather than pick when the plan doesn't cover a decision.
