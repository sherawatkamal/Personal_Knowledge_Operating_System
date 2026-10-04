# Phase One Implementation Plan

Status: **revision 3, approved**. Step 1 done; step 2 next.

Phase one: connect Granola, Gmail, Google Calendar and (last) Slack; store episodes with provenance; extract facts and commitments; run simple hybrid retrieval with citations; and measure everything with an eval harness that compares configurations. It must be usable on its own.

Not in phase one: bitemporal reconciliation, entities and entity resolution, communities and Leiden, Personalized PageRank, the query planner, agents, the MCP server, and any interface beyond one page with an ask box and cited answers.

Revision 3 applies the answers to X2–X5, fixes F1–F4 and S1–S2 on top of revision 2. **§4** records the reasoning behind decisions, including where I adjusted a fix. Closed decisions are in **§5**.

---

## 1. Repo structure

```
.
├── docker-compose.yml       # postgres+pgvector and the app container; migrations run on start; `docker compose up` is the install
├── Dockerfile               # the Python app image (uv-managed deps)
├── pyproject.toml           # deps, ruff, pytest config
├── .env.example             # every setting with a placeholder; the real .env is gitignored
├── .gitignore               # .env, data/, secrets, tokens, dumps, eval results
├── pkos                     # shell wrapper: `./pkos sync` → `docker compose run --rm app pkos sync`
├── CLAUDE.md                # stack, commands, schema, conventions (kept current)
├── README.md                # privacy model first, setup, OAuth walkthroughs, recorded eval results
├── PLAN.md                  # this file; checklist updated as work lands
├── PROPOSAL.md              # the design document
├── migrations/              # numbered plain-SQL files, applied in order, tracked in schema_migrations
├── configs/
│   ├── systems/             # one TOML per eval/ask configuration (fts, vector, hybrid, facts, ...); committed, no personal data
│   └── llm.toml             # model profiles (hosted-answer, hosted-extract, hosted-judge, local-*) and the pricing table
├── src/pkos/
│   ├── cli.py               # entry point: health, migrate, sync, purge, embed, extract, find, ask, eval, delete, usage
│   ├── config.py            # typed settings from env; secret values never appear in repr or logs
│   ├── db.py                # connection pool and migration runner
│   ├── episodes/            # episode shape, normalisation, content hash, idempotent upsert, tombstones, purge (careful code)
│   ├── connectors/          # granola.py, gmail.py, gcal.py, slack.py behind one Connector interface
│   ├── google_auth.py       # shared OAuth2 installed-app flow and token storage for Gmail and Calendar
│   ├── llm/                 # thin model abstraction: protocol, hosted impl, Ollama impl, usage accounting
│   ├── embed/               # Embedder protocol, local impl, chunker
│   ├── extract/             # filter rules, structural extraction, semantic extraction, cache, span location
│   ├── retrieve/            # full text, vector, RRF fusion over chunks and facts; reads only through live_* views
│   ├── answer/              # answer prompt, citation parsing and validation, abstention
│   ├── eval/                # CSV loader, skip logic, runner over configurations, scorers, judge cache, comparison tables
│   ├── dump/                # HTML extraction dump viewer (step 8)
│   └── web/                 # FastAPI: one ask page, plus an episode page with spans highlighted
├── tests/
│   ├── fixtures/            # synthetic API payloads and episodes only; never real data
│   ├── unit/                # hashing, normalisation, span location, RRF, scoring, config loading
│   └── integration/         # against a real Postgres test DB: idempotency, cascades, tombstones, purge, cache
└── data/                    # GITIGNORED: questions.csv, eval results, dumps, OAuth tokens, model downloads
```

---

## 2. Database schema

Plain SQL. Postgres 17 in Docker (`UNIQUE NULLS NOT DISTINCT` needs 15+). Every derived row references `episodes` with `ON DELETE CASCADE`.

Key design points in this revision:
- **`body` is source text only** (P2). Nothing generated is written into it. All character spans are offsets into `body`. Structural facts point at a field (`field_ref`) instead of an offset.
- **`content_hash`** is sha256 over a canonical JSON of the normalised fields that matter (title, occurred_at, ends_at, participants, body, and the stable part of `meta`). It never covers rendered text, and it never covers volatile fields such as Gmail read/unread labels. Otherwise marking an email read would re-trigger extraction.
- **Soft delete** (D11): upstream deletions set `deleted_at`. Retrieval reads only through `live_*` views. `purge` hard-deletes tombstones older than the configured window, which cascades.
- **Multiple extraction sets side by side** (P1, approach A): see the decision note under the facts table.

Migrations are numbered in the order the build creates them (see §3).

```sql
-- Created by the migration runner itself before it reads any file (it must exist to know what has run):
-- CREATE TABLE IF NOT EXISTS schema_migrations (
--     version     text PRIMARY KEY,
--     checksum    text NOT NULL,                   -- sha256 of the file; runner refuses if an applied file was edited
--     applied_at  timestamptz NOT NULL DEFAULT now()
-- );

-- 0001_base.sql  (step 1)
CREATE EXTENSION IF NOT EXISTS vector;

-- 0002_episodes.sql  (step 2)
CREATE TABLE sync_state (
    source           text PRIMARY KEY,               -- 'granola' | 'gmail' | 'gcal' | 'slack'
    watermark        jsonb NOT NULL DEFAULT '{}'::jsonb,  -- gmail {"history_id"}, gcal {"sync_token"}, granola {"updated_after","cursor"}, slack {"<channel>": "<ts>"}
    last_success_at  timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE episodes (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source         text NOT NULL CHECK (source IN ('granola','gmail','gcal','slack')),
    external_id    text NOT NULL,                    -- Gmail message id, Calendar event instance id, Granola note id, Slack channel:thread_ts
    thread_id      text,                             -- Gmail thread id, Calendar recurring event id, Slack channel id
    occurred_at    timestamptz NOT NULL,             -- sent time, event start, note time, thread root time
    ends_at        timestamptz,                      -- event end; NULL elsewhere
    title          text,                             -- subject, event summary, note title, channel name
    participants   jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{"name","email","handle","role":"from|to|cc|bcc|organizer|attendee|creator|author","response"}]
    refs           jsonb NOT NULL DEFAULT '[]'::jsonb,  -- in-reply-to, references, linked event ids, urls
    meta           jsonb NOT NULL DEFAULT '{}'::jsonb,  -- source-specific normalised fields: location, labels, status, channel type
    body           text NOT NULL DEFAULT '',         -- SOURCE TEXT ONLY; all character spans are offsets into this string
    sections       jsonb NOT NULL DEFAULT '[]'::jsonb,  -- layout of body: [{"name":"my_notes","start":0,"end":812}, {"name":"transcript",...}]
    raw            jsonb NOT NULL,                   -- source payload as received
    content_hash   text NOT NULL,                    -- sha256 of canonical normalised fields; see notes above
    filter_status  text NOT NULL DEFAULT 'pending' CHECK (filter_status IN ('pending','kept','dropped')),
    filter_reason  text,
    deleted_at     timestamptz,                      -- tombstone from upstream deletion; NULL = live
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT episodes_source_external_uq UNIQUE (source, external_id),
    CONSTRAINT episodes_hash_format CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT episodes_time_order CHECK (ends_at IS NULL OR ends_at >= occurred_at)
);
CREATE INDEX episodes_occurred_idx      ON episodes (occurred_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX episodes_hash_idx          ON episodes (content_hash);      -- deliberately NOT unique (D4)
CREATE INDEX episodes_thread_idx        ON episodes (source, thread_id) WHERE thread_id IS NOT NULL;
CREATE INDEX episodes_participants_idx  ON episodes USING gin (participants jsonb_path_ops);
CREATE INDEX episodes_tombstone_idx     ON episodes (deleted_at) WHERE deleted_at IS NOT NULL;
CREATE INDEX episodes_extract_queue_idx ON episodes (occurred_at DESC) WHERE filter_status = 'kept' AND deleted_at IS NULL;

CREATE VIEW live_episodes AS SELECT * FROM episodes WHERE deleted_at IS NULL;

-- 0003_chunks.sql  (step 4)
-- kind='body': a slice of body with real offsets.
-- kind='meta': one per episode, rendered from title/time/participants/location so that a calendar
--              event with no description, or an email's sender, is findable at all. It lives here,
--              never in body, so it does not affect content_hash or spans. See §4 X1.
CREATE TABLE episode_chunks (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    episode_id       bigint NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    content_hash     text NOT NULL,                  -- episode version this chunk was built from
    kind             text NOT NULL CHECK (kind IN ('body','meta')),
    chunk_index      integer NOT NULL,
    char_start       integer,
    char_end         integer,
    section          text,                           -- from episodes.sections, for body chunks
    text             text NOT NULL,
    embedding        vector(384),
    embedding_model  text,
    tsv              tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    CONSTRAINT chunks_offsets CHECK (
        (kind = 'body' AND char_start >= 0 AND char_end > char_start) OR
        (kind = 'meta' AND char_start IS NULL AND char_end IS NULL)),
    CONSTRAINT chunks_episode_index_uq UNIQUE (episode_id, kind, chunk_index)
);
CREATE INDEX chunks_episode_idx   ON episode_chunks (episode_id);
CREATE INDEX chunks_embedding_idx ON episode_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_tsv_idx       ON episode_chunks USING gin (tsv);

CREATE VIEW live_chunks AS
  SELECT c.* FROM episode_chunks c JOIN episodes e ON e.id = c.episode_id
  WHERE e.deleted_at IS NULL AND c.content_hash = e.content_hash;

-- 0004_runs_and_usage.sql  (step 5; moved out of step 1 per your reordering)
CREATE TABLE runs (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('sync','purge','embed','extract','eval','ask')),
    source       text,
    config       text,                               -- system config name for eval/ask
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','failed','budget_stopped')),
    stats        jsonb NOT NULL DEFAULT '{}'::jsonb,
    error        text                                -- scrubbed before write
);
CREATE INDEX runs_kind_started_idx ON runs (kind, started_at DESC);

CREATE TABLE model_calls (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id         bigint REFERENCES runs(id) ON DELETE SET NULL,
    purpose        text NOT NULL CHECK (purpose IN ('extract','answer','judge','embed')),
    provider       text NOT NULL,
    model          text NOT NULL,
    input_tokens   integer NOT NULL DEFAULT 0,
    output_tokens  integer NOT NULL DEFAULT 0,
    cost_usd       numeric(12,6) NOT NULL DEFAULT 0,
    latency_ms     integer,
    episode_id     bigint,                           -- no FK on purpose: cost history holds no content and outlives purges
    ok             boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX model_calls_run_idx     ON model_calls (run_id);
CREATE INDEX model_calls_created_idx ON model_calls (created_at DESC);

-- 0005_extraction.sql  (step 8)
CREATE TABLE predicates (
    name           text PRIMARY KEY,
    description    text NOT NULL,
    subject_types  text[] NOT NULL,
    object_types   text[] NOT NULL,
    qualifiers     text[] NOT NULL DEFAULT '{}',     -- allowed qualifier keys, e.g. {to} for introduced
    cardinality    text NOT NULL CHECK (cardinality IN ('one','many'))
);
-- seeded with the 20 predicates in §5 D6

-- One row per distinct extractor: deterministic structural code, or a (provider, model, prompt, schema) combination.
CREATE TABLE extractor_versions (
    id           text PRIMARY KEY,                   -- e.g. 'structural-v1', 'sem-v3-hosted', 'sem-v3-local'
    method       text NOT NULL CHECK (method IN ('structural','semantic')),
    provider     text,
    model        text,
    prompt_sha   text,
    schema_sha   text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Key = sha256 of the exact rendered model input + model + prompt_sha + schema_sha.
-- Keying on the rendered input (not episodes.content_hash) means a volatile field that never
-- reaches the prompt can't cause a cache miss, and a prompt edit always does.
CREATE TABLE extraction_cache (
    cache_key          text PRIMARY KEY,
    extractor_version  text NOT NULL REFERENCES extractor_versions(id),
    output             jsonb NOT NULL,               -- schema-validated output, before span location
    created_at         timestamptz NOT NULL DEFAULT now()
);

-- Which cache entries each episode used: lets purge remove cache rows nothing else needs.
CREATE TABLE extraction_cache_refs (
    cache_key   text NOT NULL REFERENCES extraction_cache(cache_key) ON DELETE CASCADE,
    episode_id  bigint NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    PRIMARY KEY (cache_key, episode_id)
);
CREATE INDEX cache_refs_episode_idx ON extraction_cache_refs (episode_id);

CREATE TABLE facts (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    episode_id         bigint NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    content_hash       text NOT NULL,                -- episode version extracted from
    extractor_version  text NOT NULL REFERENCES extractor_versions(id),
    method             text NOT NULL CHECK (method IN ('structural','semantic')),
    subject_text       text NOT NULL,
    subject_type       text NOT NULL CHECK (subject_type IN ('person','organization','project','place','topic','document','event')),
    predicate          text NOT NULL REFERENCES predicates(name),
    object_text        text NOT NULL,
    object_type        text NOT NULL CHECK (object_type IN ('person','organization','project','place','topic','document','event','literal','date')),
    object_date        date,
    qualifiers         jsonb NOT NULL DEFAULT '{}'::jsonb,
    valid_from         date,                         -- only when the text states it (D5)
    valid_to           date,
    recorded_at        timestamptz NOT NULL DEFAULT now(),
    -- provenance: exactly one of (character span into body) or (field reference)
    span_start         integer,
    span_end           integer,
    field_ref          text,                         -- e.g. 'header:from', 'header:to', 'attendees', 'organizer', 'recurrence'
    section            text,                         -- body section the span lies in (D13); NULL for field refs
    evidence_text      text NOT NULL,                -- body[span_start:span_end] verified before insert, or the referenced field's value
    confidence         real NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    fact_text          text NOT NULL,                -- rendered sentence used for retrieval
    embedding          vector(384),
    tsv                tsvector GENERATED ALWAYS AS (to_tsvector('english', fact_text || ' ' || evidence_text)) STORED,
    CONSTRAINT facts_provenance_xor CHECK (
        (span_start IS NOT NULL AND span_end IS NOT NULL AND span_start >= 0 AND span_end > span_start AND field_ref IS NULL)
     OR (span_start IS NULL AND span_end IS NULL AND field_ref IS NOT NULL)),
    CONSTRAINT facts_structural_uses_fields CHECK (method = 'semantic' OR field_ref IS NOT NULL),
    CONSTRAINT facts_valid_order CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from),
    CONSTRAINT facts_dedup_uq UNIQUE NULLS NOT DISTINCT
        (episode_id, content_hash, extractor_version, predicate, subject_text, object_text, span_start, field_ref)
);
CREATE INDEX facts_episode_idx    ON facts (episode_id);
CREATE INDEX facts_version_idx    ON facts (extractor_version);
CREATE INDEX facts_predicate_idx  ON facts (predicate);
CREATE INDEX facts_subject_idx    ON facts (lower(subject_text));
CREATE INDEX facts_object_idx     ON facts (lower(object_text));
CREATE INDEX facts_valid_idx      ON facts (valid_from, valid_to) WHERE valid_from IS NOT NULL OR valid_to IS NOT NULL;
CREATE INDEX facts_embedding_idx  ON facts USING hnsw (embedding vector_cosine_ops);
CREATE INDEX facts_tsv_idx        ON facts USING gin (tsv);

CREATE VIEW live_facts AS
  SELECT f.* FROM facts f JOIN episodes e ON e.id = f.episode_id
  WHERE e.deleted_at IS NULL AND f.content_hash = e.content_hash;

CREATE TABLE commitments (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    episode_id         bigint NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    content_hash       text NOT NULL,
    extractor_version  text NOT NULL REFERENCES extractor_versions(id),
    promisor           text NOT NULL,
    promisee           text,
    obligation         text NOT NULL,
    due_date           date,                         -- resolved against episode occurred_at ("by Friday")
    due_text           text,                         -- the phrase as written
    state              text NOT NULL DEFAULT 'open' CHECK (state IN ('open','done','cancelled','unknown')),  -- as stated in this episode only; no tracking
    span_start         integer NOT NULL,
    span_end           integer NOT NULL,
    section            text,
    evidence_text      text NOT NULL,
    confidence         real NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    recorded_at        timestamptz NOT NULL DEFAULT now(),
    embedding          vector(384),
    tsv                tsvector GENERATED ALWAYS AS (to_tsvector('english', obligation || ' ' || evidence_text)) STORED,
    CONSTRAINT commitments_span_ok CHECK (span_start >= 0 AND span_end > span_start),
    CONSTRAINT commitments_dedup_uq UNIQUE (episode_id, content_hash, extractor_version, promisor, obligation, span_start)
);
CREATE INDEX commitments_episode_idx   ON commitments (episode_id);
CREATE INDEX commitments_version_idx   ON commitments (extractor_version);
CREATE INDEX commitments_due_idx       ON commitments (due_date) WHERE due_date IS NOT NULL;
CREATE INDEX commitments_embedding_idx ON commitments USING hnsw (embedding vector_cosine_ops);
CREATE INDEX commitments_tsv_idx       ON commitments USING gin (tsv);

CREATE VIEW live_commitments AS
  SELECT c.* FROM commitments c JOIN episodes e ON e.id = c.episode_id
  WHERE e.deleted_at IS NULL AND c.content_hash = e.content_hash;
```

### P1 decision: keep extraction sets side by side, version in the constraint (your option A)

`extractor_version` is in the dedup constraint, and fact retrieval takes the semantic extractor version as a **required** parameter, with no default. **Why A over delete-and-replace:** A2 and A3 need `facts-hosted` and `facts-local` to be runnable as configurations, not code changes. With delete-and-replace, switching configuration would mean re-materialising one set over the other before every eval run. That costs no model calls (it comes from the cache), but it does mean re-embedding every fact, and you could never run the two configurations in the same `pkos eval` invocation.

The risk with A is a query that forgets the version filter and double-counts facts. Mitigations:
1. One function, `retrieve.facts(..., semantic_version=...)`, is the only code path that reads `facts`, and the parameter is required.
2. A test asserts that no other module queries `facts` directly.
3. A test with two loaded versions asserts results are drawn from exactly one.

Old versions are removed explicitly with `pkos facts prune --keep <ids>`.

Two rules apply on top:
- **When an episode's content changes**, facts and commitments for the old `content_hash` are deleted in the same transaction as the episode update, for every version. They describe text that no longer exists.
- **Inserts never use `ON CONFLICT DO NOTHING`.** Duplicates within one extraction are merged in Python first, so a constraint violation is a loud bug, never a silent drop.

### Deletion semantics (careful code, with tests)

| Event | Effect |
|---|---|
| Upstream item deleted (Gmail permanent delete or moved to Trash, Calendar event cancelled/deleted, Granola note deleted, Slack message deleted) | `deleted_at = now()`. Excluded from all retrieval through `live_*` views. Derived rows kept. |
| Upstream item restored inside the window (un-trashed, re-created) | `deleted_at = NULL`, and normal hash check. |
| Gmail archive (INBOX label removed) | **Nothing.** Archived mail is still in All Mail and in scope. Tested explicitly, because this is the "inbox cleanup" case. |
| `purge` (only on `pkos purge` or `pkos sync --purge`; plain `sync` prints what is eligible and destroys nothing) | Hard-deletes episodes with `deleted_at < now() - PURGE_AFTER_DAYS` (default 30, configurable). FK cascade removes chunks, facts, commitments and cache refs. Then deletes `extraction_cache` rows with no remaining refs. |
| `pkos delete --source X` / `--episode N` (you, locally) | Immediate hard delete with the same cascade. No tombstone: you asked for it gone. |
| Episode content changed | Old-hash chunks, facts and commitments deleted in the same transaction; re-embed and re-extract from cache or model. |

Note: Gmail empties Trash after 30 days, so the default window matches Gmail's own behaviour.

---

## 3. Build order

Each step ends with a **STOP**: I show you the result, commit, and wait.

### Step 1: Skeleton

Status: **done 2026-10-04.** Verified from a clean Docker state (0 images, 0 volumes) and from a cold GitHub clone with `.env.example` copied: `docker compose up` reaches healthy in about 35 s, `./pkos health` is all ok, and `./pkos test` passes 30/30 in the container. Pinned images (all native linux/arm64): `python:3.12.15-slim-trixie`, `ghcr.io/astral-sh/uv:0.12.23`, `pgvector/pgvector:0.8.7-pg17` (Postgres 17.11, pgvector 0.8.7).

| | Item | |
|---|---|---|
| - [x] | `git init`, `.gitignore`, `.env.example` | **essential** |
| - [x] | `pyproject.toml`, `Dockerfile`, `docker-compose.yml` (pinned pgvector 0.8.7 on Postgres 17.11, named volume, healthcheck) | **essential** |
| - [x] | `./pkos` wrapper, so the single sync command is `./pkos sync`; warns if FileVault is off, on `health` and first run only (S2) | **essential** |
| - [x] | Migration runner (checksums, refuses edited migrations) and `0001_base.sql`; runs on container start | **essential** |
| - [x] | Runner idempotency tests: migrate twice is a no-op, an edited applied migration is refused, a failed migration rolls back | **essential** |
| - [x] | `pkos health`: DB reachable, extensions present, migrations current | **essential** |
| - [x] | `./pkos test`: pytest in the container with a fresh `pkos_test_*` database per test | **essential** |
| - [x] | Secret-scrubbing log filter, plus a test that a planted fake key never reaches log output or exception text | **essential** |
| - [x] | CLAUDE.md first version | **essential** |
| — | `runs`, `model_calls`, usage reporting | deferred → step 5 |
| — | Provider reachability in `health` (hosted, Ollama) | deferred → step 4 |
| — | HTML dump viewer | deferred → step 8 |
| — | Episode upsert idempotency tests | deferred → step 2 (see X5) |
| - [x] | **STOP** → commit | |

### Step 2: Episodes and Granola
- [ ] `0002_episodes.sql` (final shape above, with P2 applied before any data exists)
- [ ] Normalisation and canonical content hash, with tests: deterministic, Unicode and whitespace normalised, volatile fields excluded
- [ ] Idempotent upsert keyed on `(source, external_id)`. Tests written **before** the connector: sync twice gives zero writes; changed content updates in place and clears old-hash derived rows; identical content from two items gives two episodes; a crash mid-sync leaves the watermark unadvanced
- [ ] Tombstone and restore functions, with tests (purge comes in step 5, but connectors need the tombstone logic now)
- [ ] Verify Granola's actual API against current docs
- [ ] Granola connector: pagination, `updated_after` watermark, deletions to tombstones, synthetic fixtures
- [ ] `./pkos sync granola` on your account; counts only in the terminal
- [ ] **Report on D13**: which fields the API returns (your notes, enhanced notes, transcript, attendees, calendar link), and whether enhanced notes are model-generated. `sections` layout is proposed for your OK; extraction isn't wired to it until you agree
- [ ] **STOP** → commit

### Step 3: Eval harness
- [ ] CSV loader: columns `id,question,class,answerable,gold_answer,gold_sources,judge,sources_needed,split`; classes `lookup|relational|temporal|aggregate|obligational`; `split` is `test` (150, frozen, reported) or `dev` (30, for tuning); loud failure on malformed rows
- [ ] Split rules (F1): `pkos eval --split dev|test` (default `dev`); published comparison tables are built from `test` only, and the code path that writes README tables refuses dev results
- [ ] Test-set ledger (F1): every `test` run appends (config name, config sha256, timestamp) to `data/eval/test_ledger.jsonl`. Scoring `test` with a config whose hash changed after it already has a `test` result prints a loud warning, and the result is marked `†` in every table it appears in, with the number of test-set looks per config. Thresholds, prompts and chunk sizes are tuned on `dev` only (see §4 on why warn-and-mark rather than refuse)
- [ ] Skip logic (P3). Each question gets one status: `scored`, or skipped with a reason:
  - `source_not_connected`: a source in `sources_needed` has never synced successfully
  - `gold_not_found`: the source is connected but a gold id is missing. This is distinct because it's usually a typo in the CSV, so it's listed by question id
  - `gold_tombstoned`: the gold episode is soft-deleted

  Never an error. The table header reports scored/skipped counts by reason.
- [ ] Configuration loading from `configs/systems/*.toml` (design below). The harness takes configurations, never system names
- [ ] Runner over N configurations in one invocation, with a null config (always abstains) to prove the pipeline end to end
- [ ] Scorers: correctness (exact or model judge with gold answer and gold sources attached), recall@k on gold sources, citation precision, abstention correctness, tokens, p50/p95 latency, cost
- [ ] Judge cache keyed by hash of (question, answer, gold answer, gold source ids, judge model, judge prompt)
- [ ] Freeze check: results record the CSV sha256 and the split assignment; the run warns loudly if either differs from what `pkos eval --freeze` recorded in `data/eval/frozen.json`. Changing a question's split after freezing is reported as a freeze violation
- [ ] Comparison output (design below), plus full per-question results to `data/eval/<timestamp>/`
- [ ] Until step 5, token and cost totals come from in-process accounting in the `llm/` layer, printed at the end of each command
- [ ] **STOP** → commit

### Step 4: Baseline over raw episodes
- [ ] `llm/`: `complete(messages, schema=None) -> Result(text|json, usage)`, with hosted and Ollama implementations and profiles from `configs/llm.toml`. Business logic never imports a provider SDK, and a test enforces that
- [ ] Provider checks added to `pkos health`
- [ ] `embed/`: Embedder protocol, fastembed `bge-small-en-v1.5` impl, offset-preserving chunker, one `meta` chunk per episode
- [ ] `./pkos embed`: only episodes whose current hash has no chunks (zero work on re-run)
- [ ] Retrievers over `live_chunks`: full text, vector, RRF
- [ ] Answer stage: cite `[E123]`; citations validated against the retrieved set; abstain below threshold
- [ ] `./pkos find "<text>"`, a model-free id lookup for writing gold sources
- [ ] `./pkos ask "..." [--config hybrid]` and the web page (ask box; episode page with span highlighted)
- [ ] Score `fts`, `vector`, `hybrid`, `hybrid-local` (Granola-answerable questions only; others skip as `source_not_connected`). Record in README
- [ ] **STOP** → commit

### Step 5: Processing log and usage (moved here from step 1)
- [ ] `0004_runs_and_usage.sql`; every command opens a run; `llm/` writes a `model_calls` row per real call (cache hits write none)
- [ ] Pricing table in `configs/llm.toml`; cost computed per call
- [ ] `pkos usage [--since] [--by run|purpose|model|source]`: tokens and cost
- [ ] `pkos purge` and `pkos sync --purge` implemented and tested (purge records its runs here); plain `sync` only reports what is eligible for purge (S1)
- [ ] **STOP** → commit

### Step 6: Gmail
- [ ] `google_auth.py`: installed-app flow with **both** Gmail and Calendar read-only scopes requested once, so you consent once. Token in `data/secrets/google_token.json`, mode 0600
- [ ] Initial sync from the fixed floor **2025-10-04** (D12), never rolling, then `history.list` from `historyId`; 404 fallback to a bounded re-list
- [ ] Message-level episodes, `thread_id`, quoted-reply text stripped from `body` (kept in `raw`); category and label exclusions applied at list time
- [ ] Trash/permanent delete → tombstone; un-trash → restore; archive → nothing (tested)
- [ ] README: Google Cloud project, OAuth client (Desktop), test user, scopes
- [ ] Re-run all baseline configs; record the post-Gmail numbers
- [ ] **STOP** → commit

### Step 7: Google Calendar
- [ ] `events.list` with `singleEvents=true` (one episode per instance, `thread_id` = recurring event id), incremental via `syncToken`; 410 Gone falls back to a full resync, which is idempotent
- [ ] Participants from organizer and attendees, with `responseStatus` kept; `body` = event description only; location and conference data in `meta`
- [ ] Cancelled events → tombstone
- [ ] Scope (D19): all calendars you own (`accessRole = owner`), excluding holiday and subscription calendars; from 2025-10-04 to now + 3 months, where only the forward edge rolls; declined events kept with `response` recorded
- [ ] Future events synced and structurally extracted, but not semantically extracted while in the future (F4, implemented in step 8)
- [ ] Re-run baseline configs; record
- [ ] **STOP** → commit

### Step 8: Fact and commitment extraction
- [ ] `0005_extraction.sql`, with predicates seeded from §5 D6
- [ ] Filter stage (rules: List-Unsubscribe, Precedence: bulk, no-reply senders, receipts; Calendar events with empty descriptions skip semantic extraction). Dropped episodes stay searchable
- [ ] Future events (F4): the semantic queue only takes episodes with `occurred_at <= now()` at extraction time. A future event becomes eligible on the first `extract` after it happens, so a rescheduled event is extracted once, in its final form. Structural facts are extracted for all events, past and future. Tested with a clock fixture
- [ ] Structural extractor `structural-v1`: `sent_to`, `authored`, `invited_to`, `organized`, `instance_of` from headers and attendee fields, using `field_ref` (P2), confidence 1.0
- [ ] Semantic extractor: one call per kept episode returns facts **and** commitments. Content is fenced as data. JSON schema generated from `predicates`. The episode date is passed in so relative dates can be resolved. Per D13, sections marked as model-generated are excluded or deprioritised as agreed in step 2
- [ ] Span location: the model quotes and code finds the offset (exact match, then whitespace-normalised match within the declared section). Unfound quotes are rejected and counted
- [ ] Cache check before every call; test: a second run over unchanged data makes **zero** model calls
- [ ] `--budget-usd`, newest first; per-run token and cost summary
- [ ] Provenance tests: no fact without an episode; XOR constraint holds; a content change clears old-hash facts for all versions; purge and hard delete cascade; two versions coexist and retrieval sees exactly one
- [ ] Dump viewer (`src/pkos/dump/`), designed against real output: `data/dumps/extract-<run>.html` showing each episode's text with spans highlighted, field-ref facts listed, commitments, confidences, and rejected quotes
- [ ] Prompt iteration on the dev-relevant sample only (episodes behind `dev` gold sources plus a random fill to 200), never tuned against `test` gold sources
- [ ] Run on 200 items with the hosted extractor and the same 200 with the local extractor; report cost per 1,000 items and wall-clock time for both
- [ ] **STOP**: you review the dump
- [ ] **Extraction prompt frozen for the full run** (F2): the prompt, schema, predicate seed and both model ids are recorded as `sem-vN-hosted` and `sem-vN-local`. Full extraction for either version starts only after this box is ticked. Any later change to prompt, schema, predicates or model invalidates that version's cache; before making one I'll give you the cost: hosted $ and local wall-clock hours, estimated from the measured per-item numbers

### Step 9: Retrieval over facts, full comparison
- [ ] Embed `fact_text` and commitment text (only rows without embeddings)
- [ ] Fact retriever over `live_facts` and `live_commitments`, version required, with source episodes attached for the answer stage
- [ ] Full extraction runs for both frozen versions (hosted first; local in the background, possibly overnight to days, see X6)
- [ ] Thresholds for the facts configurations calibrated on `dev`, then configs committed before the first `test` run
- [ ] Run every configuration on `test` in one `pkos eval --split test --all`; README gets the full comparison table, including configurations that lost
- [ ] Phase-one gate, stated plainly: does `facts+chunks` beat `fts` per class?
- [ ] Hosted vs local quality and cost gap published in README (A3)
- [ ] **STOP** → commit

### Step 10: Slack (last, by design: tests that a new source touches no extraction or retrieval code)
- [ ] Slack app with a user token, read-only history scopes; verify current rate limits for internally-built apps
- [ ] Episode granularity and migration rule (F3), with tests written **before** the connector:
  - a thread (root + replies) is one episode, `external_id = channel:thread:<root_ts>`
  - unthreaded messages are grouped into a channel-day episode, `external_id = channel:day:<YYYY-MM-DD>`, with days cut in the fixed `PKOS_TZ` set at install (changing it later would re-key every day window, so it is documented as install-time only)
  - when a message in a day window acquires a reply, one transaction removes it from the day episode (whose hash changes, clearing that episode's old derived rows) and creates or updates the thread episode. No text exists under two live external ids at any commit point
  - **sticky**: once a message is a thread root it stays one, even if all replies are deleted. This prevents flapping between keys
  - "also send to channel" thread replies belong to the thread only, and are skipped when building day windows
  - a day window left empty after migration is hard-deleted, not tombstoned: its text still exists, just under the thread key
  - tests: migration in one sync, migration across two syncs, a broadcast reply, all replies deleted (stays threaded), sync twice gives zero writes, and an invariant check that no Slack message ts appears in two live episodes
- [ ] Incremental by per-channel latest `ts`; edits change the hash, so the episode updates; deletions → tombstone
- [ ] Extraction and retrieval code unchanged: diff checked and reported. If Slack forced a change there, that's a finding and goes in the README
- [ ] Extract Slack with the frozen extractor versions; re-run baseline and facts configs on `test`; record
- [ ] **STOP** → commit

---

## Harness configuration design (A2)

A configuration is a TOML file. Every ablation is a file, not a code path. Example:

```toml
# configs/systems/facts-chunks-hosted.toml
name = "facts-chunks-hosted"
description = "Facts and commitments fused with raw chunks; hosted extraction and answering"

[retrieval]
corpora = ["chunks", "facts", "commitments"]   # any subset
modes   = ["fts", "vector"]                    # any subset; >1 is fused
fusion  = "rrf"
rrf_k   = 60
k       = 20                                    # final candidates passed to the answer stage

[facts]
semantic_version   = "sem-v1-hosted"           # REQUIRED when corpora includes facts or commitments
structural_version = "structural-v1"

[answer]
llm = "hosted-answer"                           # profile name in configs/llm.toml
abstain_min_score = 0.0                         # calibrated on the dev split only (F1)
```

Shipped configurations:

| config | corpora | modes | extractor | answer LLM | available from |
|---|---|---|---|---|---|
| `null` | — | — | — | — | step 3 |
| `fts` | chunks | fts | — | hosted | step 4 |
| `vector` | chunks | vector | — | hosted | step 4 |
| `hybrid` | chunks | fts+vector | — | hosted | step 4 |
| `hybrid-local` | chunks | fts+vector | — | local | step 4 |
| `facts` | facts+commitments | fts+vector | hosted | hosted | step 9 |
| `facts-chunks` | facts+commitments+chunks | fts+vector | hosted | hosted | step 9 |
| `facts-chunks-local` | facts+commitments+chunks | fts+vector | **local** | **local** | step 9 (fully offline pipeline) |

Rules that keep the comparison honest:
- **The judge is fixed across all configurations** (one judge profile, set in the harness, not in system configs). The judge is a hosted model even when scoring the local configuration. "Fully offline" describes the product, not the measurement, and a different judge per configuration would make the numbers incomparable.
- **Comparisons only use questions scored in every configuration being compared.** The table shows `n` per class so a skip difference can't masquerade as a quality difference.
- **Each result records** the corpus snapshot (episode counts per source, max `occurred_at`), the CSV hash, the config file hashes, and the git commit.

Output (`pkos eval --config a --config b ...` or `--all`):

```
Question set 3f9a…c21 (frozen ✓)   split=test   scored 141 / 150   skipped: source_not_connected 6, gold_not_found 2 [q041,q087], gold_tombstoned 1

ACCURACY            lookup  relational  temporal  aggregate  obligational  overall
                    n=38    n=31        n=30      n=19       n=23          n=141
fts                 0.55    0.29        0.23      0.21       0.30          0.34
hybrid              ...
facts-chunks        ...

RETRIEVAL & COST    recall@10  cite_prec  abstain_ok  false_ans_unans  tok/q  p50ms  p95ms  $/q
fts                 ...
```

The CSV and JSON forms of both tables go next to the per-question results.

---

## 4. Reasoning record

Kept from revision 2 for the record (all resolved): **X1** metadata chunk per episode; **X6** the local run will be slow and the wall-clock time is a published result; **X7** all retrieval reads `live_*` views, so tombstones can't leak; **X8** Calendar and Slack questions use `sources_needed`.

**On F1: warn-and-mark rather than refuse.** A hard refusal to score `test` with a changed config would also block legitimate changes. Step 9 adds new configurations, and a bug fix in retrieval code changes behaviour without changing any config hash. What actually inflates numbers is looking at a `test` score and then changing something. So the harness keeps a ledger of `test` looks per config. A re-look after a change is loud on the terminal and marked `†` in every table, with the look count, so a reader can see it. Two further notes:
- 30 dev questions over 5 classes is about 6 per class. That's fine for setting one abstention threshold, but thin for anything finer. Please make sure **dev includes unanswerable questions** (I'd suggest at least 6), or the abstention threshold can't be calibrated at all.
- The split must be decided before any result is seen, as you said. The freeze records it, so moving a question between splits later shows up as a violation.

**On F2:** no problem with it. One addition: a model id change also invalidates the cache, not only a prompt change, so the cost warning covers prompt, schema, predicate seed and model. Structural extractor changes cost nothing and aren't gated.

**On F3:** your rule is adopted, plus three edge cases it didn't cover:
- **Stickiness.** A thread whose replies are all deleted doesn't move back to the day window; otherwise it flaps.
- **"Also send to channel" replies.** They appear in channel history as well as in the thread, so they'd otherwise exist twice.
- **The emptied day window.** When migration leaves a day window empty, it is hard-deleted rather than tombstoned, because its text survives under the thread key.

One cost to know about: when a message migrates, its day window's hash changes, so that day window is re-extracted once. That's acceptable, and it's the price of never holding duplicate text.

**On F4:** no problem with it. Implementation detail: "future" means `occurred_at > now()` at extraction time, so an event becomes eligible automatically once it has happened. No extra job or state is needed.

**On S1:** adopted as stated.

**On S2: encryption at rest is phase two, stated in the README.**
- FileVault covers the stolen-disk threat.
- An encrypted volume adds nothing beyond it on a single-user machine.
- Application-level encryption would break full-text and vector search.

The `./pkos` wrapper runs on the host, where it can check `fdesetup status`. It warns if FileVault is off, but only on `./pkos health` and on the first run, because a warning on every command gets ignored. The README also states that `data/` (tokens, eval results, dumps) is plaintext on disk under the same protection.

---

## 5. Decisions recorded

| # | Decision |
|---|---|
| D1 | Calendar **in** phase one, its own step after Gmail, sharing Gmail's OAuth client |
| D2 | Plain Python connectors behind a `Connector` interface; no MCP-in |
| D3 | Baseline re-run and recorded after every connector step |
| D4 | `content_hash` indexed, not unique; identity is `(source, external_id)` |
| D5 | `valid_from`/`valid_to` when stated, `recorded_at` always, no `invalidated_at`, no reconciliation |
| D7 | Commitments table, same model call, no tracking or resolution, shown only in answers |
| D8 | fastembed `bge-small-en-v1.5` (384d) in-container; Ollama native on the host |
| D9 | Facts-only and facts+chunks both scored |
| D10 | CSV as proposed plus `obligational` class and `split` column; 180 questions = 150 `test` (frozen, reported) + 30 `dev` (tuning); sha256 check |
| D11 | Soft delete with tombstones; purge after 30 days (configurable) only via `pkos purge` or `sync --purge`; local `pkos delete` is immediate |
| D12 | Gmail from the fixed date 2025-10-04 (not rolling), excluding Spam, Trash, Promotions, Social. Frozen |
| D13 | Granola fields reported in step 2 before extraction is wired; user notes and transcript preferred over model-generated notes; `section` recorded per span |
| D14 | No queue or Redis in phase one |
| D15 | CLI plus one FastAPI HTML page; no Next.js |
| D16 | Confidence stored but not used to filter until checked against the dump |
| D17 | Postgres full text, labelled "full text", not "BM25" |
| D18 | No reranker in phase one |
| P1 | Extraction versions coexist; version in the constraint; retrieval requires a version (rationale above) |
| P2 | `body` is source text only; structural facts use `field_ref`; spans nullable under an XOR constraint |
| P3 | Unresolved gold sources skip with a reason; never an error |
| D19 | Calendar: owned calendars, no holiday or subscription calendars; 2025-10-04 to now + 3 months; declined events kept with response status |
| D20 | Slack is the last step (step 10), after facts retrieval is scored |
| D21 | Episodes migration stays in step 2, after the Granola API report |
| F1 | Tuning on `dev` only; `test` ledger warns and marks re-looks after a change; dev results never published |
| F2 | Full extraction starts only after "extraction prompt frozen"; any later invalidating change is costed before it is made |
| F3 | Slack thread/day-window rule with migration in one transaction, sticky threads, broadcast handling; tests before the connector |
| F4 | Future events: structural extraction only, semantic once they've happened |
| S1 | `sync` never purges; it reports what is eligible |
| S2 | Encryption at rest is phase two: FileVault covers the stolen-disk threat, an encrypted volume adds nothing beyond it on a single-user machine, and application-level encryption would break full-text and vector search. Wrapper warns if FileVault is off, on `health` and first run only |

### D6 predicates (20)

The 17 kept from revision 1 (dropping `mentioned`, `owes`, `due_on`), plus three new ones, marked ➕.

| predicate | subject → object | qualifiers | card. | typical source |
|---|---|---|---|---|
| works_at | person → organization | | one | semantic |
| has_role | person → literal | `at` | one | semantic |
| reports_to | person → person | | one | semantic |
| works_with | person → person | | many | semantic |
| member_of | person → organization/project | | many | semantic |
| works_on | person → project | | many | semantic |
| leads | person → project | | one | semantic |
| part_of | project/organization → organization/project | | one | semantic |
| attended | person → event | | many | semantic (evidence of presence) |
| organized | person → event | | many | structural (calendar organizer) + semantic |
| occurred_on | event → date | | one | semantic (events mentioned in text) |
| located_in | person/organization/event → place | | one | semantic + calendar location |
| authored | person → document | | many | structural (sender, note creator) + semantic |
| introduced | person → person | `to` | many | semantic |
| decided | person/organization → literal | | many | semantic |
| has_contact | person → literal | `kind` | many | semantic |
| about | event/document → topic/project | | many | semantic |
| ➕ invited_to | person → event | `response` (accepted/declined/tentative/none) | many | structural (calendar attendees) |
| ➕ instance_of | event → event (series) | | one | structural (recurring event id) |
| ➕ sent_to | person → person | `role` (to/cc), `channel` | many | structural (email headers, Slack DMs) |

**Why these three:**
- **`invited_to`.** A calendar attendee list proves an invitation, not attendance. Writing calendar attendees as `attended` would put false facts into exactly the temporal questions this project exists to answer ("who was in the March review"). With calendar in scope, `attended` is reserved for evidence of presence (a Granola note listing participants, or "thanks everyone who came"), and `invited_to` carries the RSVP state.
- **`instance_of`.** Recurring meetings are the most common structure in a calendar, and in practice they're the best proxy for "a project over time": "who was in the weekly design sync in March" is a series question. Without it, 52 standup instances are 52 unrelated events.
- **`sent_to`.** This fills a gap in revision 1, not a calendar-specific one: structural extraction from email headers had no predicate to write into. The proposal says headers are "the backbone of relational questions such as who introduced whom", and the backbone needs a relation. It's free (no model call) and exact.

---

## 6. What I still need from you

**Before step 2**: Granola API key in `.env`.
**Before step 3**: `questions.csv` (180 questions with the `split` column, dev including some unanswerable questions) in `data/`. I can build the harness against a synthetic CSV if yours isn't ready.
**Before step 4**: hosted provider choice, API key, model choice for answer/extract/judge, per-run spend cap; the local model you want in Ollama (I'll suggest candidates that fit 18 GB).
**Before step 6**: Google Cloud OAuth client (Desktop app) with Gmail and Calendar APIs enabled and you as a test user; `client_secret.json` in `data/secrets/`.
**Before step 10**: Slack workspace, whether you can create an app there (or need admin approval), and which conversations are in scope (public channels you're in, private channels, DMs, group DMs) and over what window.
