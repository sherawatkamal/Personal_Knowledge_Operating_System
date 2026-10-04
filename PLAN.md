# Phase One Implementation Plan

Status: **revision 3, approved**. Steps 1–2 done. Step 3 next. Step 4b (UI) designed, awaiting approval.

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
    watermark        jsonb NOT NULL DEFAULT '{}'::jsonb,  -- gmail {"history_id"}, gcal {"sync_token"}, granola {"max_updated_at"}, slack {"<channel>": "<ts>"}
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

Status: **done 2026-10-04**; G1–G3 answered. 85 tests pass in the container. A real sync stored 10 notes; a second sync wrote nothing; a forced full re-fetch returned 10 unchanged.

- [x] `0002_episodes.sql` (final shape above, with P2 applied before any data exists)
- [x] Normalisation and canonical content hash, with tests: deterministic, Unicode and whitespace normalised, volatile fields excluded
- [x] Idempotent upsert keyed on `(source, external_id)`. Tests written **before** the connector: sync twice gives zero writes; changed content updates in place and clears old-hash derived rows; identical content from two items gives two episodes; a crash mid-sync leaves the watermark unadvanced
- [x] Tombstone and restore functions, with tests (purge comes in step 5, but connectors need the tombstone logic now)
- [x] Verify Granola's actual API against current docs
- [x] Granola connector: full listing each run (deletion by diff), details only for notes updated since the `max_updated_at` watermark (5-minute overlap), synthetic fixtures
- [x] `./pkos sync granola` on your account; counts only in the terminal
- [x] **Report on D13**: which fields the API returns (your notes, enhanced notes, transcript, attendees, calendar link), and whether enhanced notes are model-generated. `sections` layout is proposed for your OK; extraction isn't wired to it until you agree
- [ ] **STOP** → commit

#### Step 2 report: what the Granola API actually returns (D13)

Verified live on 2026-10-04 by reading structure only (keys, types, counts, lengths), never content.

**API**
- `GET /v1/notes`: cursor pagination (`notes`, `hasMore`, `cursor`). `page_size` max 30.
- `GET /v1/notes/{id}?include=transcript`: full detail. Returns 413 if the transcript is too large, in which case `GET /v1/notes/{id}/transcript` pages it.
- No deletion events, so deletions are detected by diffing the full listing.
- Rate limit is 5 requests per second.

**Your data**
- 10 notes, one owner, 2026-09-21 to 2026-10-02 (12 days). Average meeting 53 minutes.

| Field | Present in | Notes |
|---|---|---|
| `summary_text` | 10/10 | Granola's AI summary, **model-generated**. Avg 3,625 chars |
| transcript | 10/10 | Avg 36k chars once rendered. Speaker `attribution` is only `me` (microphone) or `them` (system audio). 1–3 distinct speaker names per note; half the notes have one name only, so remote speakers are often not told apart |
| `private_notes_text` | **0/10** | Your own typed notes: empty in every note |
| `attendees` | 10/10 | **Only you**, in every note |
| `calendar_event` | **0/10** | Always null: these notes aren't linked to calendar events |

**Body layout as built.** Sections are stored with exact offsets:
1. `my_notes` (when present)
2. `transcript`: consecutive same-speaker segments merged into one `Name: text` line
3. `granola_summary`: `generated: true`

The speaker label is a deterministic rendering of the source's speaker field. Timestamps stay in `raw`.

**Consequences**
- "Prefer my own notes" has nothing to prefer today. The only person-authored content is the transcript.
- Granola contributes nothing to `attended` or `invited_to`. Attendees list only you, and with no calendar link, a Granola note can't be joined to its Calendar event.
- The transcript is about 10× the summary's size, so extracting from it costs about 10× per note. That's trivial at 10 notes (≈90k input tokens) but material at scale.

**Step 2 STOP questions (answered: G1 yes, the 10 notes are the whole archive; G2 option (c); G3 approved)**
- **G1.** Is 10 notes over 12 days your whole Granola archive? If you have older notes, the key's access scope may be limiting it (the API added Personal/Public note scopes in v1.2.0). Check the key's settings in Granola.
- **G2.** Extraction sources. Your D13 rule gives: extract from `transcript` (and `my_notes` when present), and exclude `granola_summary` from extraction but keep it in search chunks.

  The catch: the transcript often labels remote speakers only as `them`. So "I'll send it Friday" from a remote person can't be attributed to a named person from the transcript alone. The summary does name people, but it's model-generated.

  Options:
  - (a) Transcript only (strict).
  - (b) Also extract from the summary, with every such fact carrying `section = granola_summary` and a confidence cap, so it's filterable.
  - (c) Transcript only, but let the extraction prompt see the summary as context for resolving names, not as a source of facts.

  I lean (c): it keeps provenance on person-authored text and uses the summary only to help name speakers.
- **G3.** Approve the body layout above. Changing it later just re-hashes the Granola episodes (10 updates, no data loss), so this is cheap to revisit.


### Step 3: Eval harness
- [ ] CSV loader: columns `id,question,class,answerable,gold_answer,gold_sources,judge,sources_needed,split,origin` (`origin` = `user` | `drafted` | `near_miss`); classes `lookup|relational|temporal|aggregate|obligational`; `split` is `test` (150, frozen, reported) or `dev` (30, for tuning); loud failure on malformed rows
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
- [ ] Answer stage: cites retrieved **items** (`[1]`, `[2]`… mapped to chunk ids, later fact ids), each resolving to an episode **and a character span**, so a citation can open at the supporting text (needed by the step 4b answer view); citations validated against the retrieved set; abstain below threshold. The full retrieved set (rank, per-mode scores, cited or not) is returned with every answer
- [ ] `./pkos find "<text>"`, a model-free id lookup for writing gold sources
- [ ] `./pkos ask "..." [--config hybrid]` and the web page (ask box; episode page with span highlighted)
- [ ] Smoke set: 10–15 Granola questions in `data/smoke.csv`, drafted under the same rules as the main set (§ Question set). These never enter the frozen set: they will have been looked at during development
- [ ] Score `fts`, `vector`, `hybrid`, `hybrid-local` on the smoke set. Recorded in README **as a smoke test, not a result**
- [ ] **STOP** → commit

### Step 5: Processing log and usage (moved here from step 1)
- [ ] `0004_runs_and_usage.sql`; every command opens a run; `llm/` writes a `model_calls` row per real call (cache hits write none)
- [ ] Pricing table in `configs/llm.toml`; cost computed per call
- [ ] `pkos usage [--since] [--by run|purpose|model|source]`: tokens and cost
- [ ] `pkos purge` and `pkos sync --purge` implemented and tested (purge records its runs here); plain `sync` only reports what is eligible for purge (S1)
- [ ] **STOP** → commit

### Step 4b: UI (designed now; built after step 5, see U1)

**Design status: awaiting approval.** Nothing is built.

Two audiences: you, debugging extraction and retrieval daily; and a stranger who clones the repo and needs to understand in 30 seconds what this does.

**Stack (D15 stands).** Six screens of lists, detail views and forms suit server-rendered HTML:
- FastAPI and Jinja2 templates, plus one hand-written CSS file.
- **htmx, vendored** as a single static file (~50 KB) in `src/pkos/web/static/`. No build step, no CDN.

Next.js would bring a Node toolchain and a build step, or a second service, and break "docker compose up is the install". htmx is used only where a full page reload is wrong: sync-status polling, the search box, and the delete-confirmation count. Every screen still works with JavaScript off, except live polling.

Assets are vendored, not loaded from a CDN, because fully offline operation is a stated commitment, and a CDN call also tells a third party when you open the app.

**Serving**
- In 4b, the `app` service changes from "print health and exit" to `uvicorn` with port `127.0.0.1:8000` published, bound to **localhost only**. `docker compose up` then means: open http://localhost:8000. Health is still logged at startup.

**Security** (single user, no login, but a local web server is reachable from any web page you visit):
- **Host header allowlist** (`localhost`, `127.0.0.1`), so a DNS-rebinding page can't read your archive.
- **CSRF token on every POST**, with a `SameSite=Strict` cookie, so a random website can't trigger "delete source" or "sync" from your browser.
- Destructive actions need a confirmation page that shows exact counts, and you type the source name to confirm.

#### Screens and routes

| # | Screen | Routes | Reads from | Background work? |
|---|---|---|---|---|
| 0 | **Home / Ask** | `GET /`, `POST /ask` → redirect to `/answers/{id}` | `configs/systems/*.toml` (config picker); `asks` (history list); live counts per source | No. Asking is synchronous (seconds) |
| 1 | **Sources** | `GET /sources`, `POST /sources/{s}/sync`, `GET /sources/{s}/status` (htmx fragment, polled) | `sync_state`, counts from `live_episodes`, tombstone counts, configured scope from Settings, latest `runs` row per source | **Yes: "Sync now"** (U3) |
| 2 | **Processing log** | `GET /log`, `GET /log/runs/{id}` | `runs`, `model_calls` (step 5) | No |
| 3 | **Episodes** | `GET /episodes?q=&source=&from=&to=&page=`, `GET /episodes/{id}?hl={start}-{end}` | `live_episodes`; search over `live_chunks` full text; later `live_facts` and `live_commitments` | No |
| 4 | **Answer** | `GET /answers/{id}` | `asks`, `ask_items` joined to `live_*` | No |
| 5 | **Settings** | `GET /settings`, `GET`/`POST /sources/{s}/delete`, `POST /purge` | Effective config (`Settings`, `configs/*.toml`); base-table counts for delete and purge previews | No (U4) |

**0. Home / Ask**
- One question box, a configuration picker (default from config), and a history list of past questions with their config and date.
- When history is empty, the page shows the stranger's view: one paragraph on what pkos is; per-source counts and last sync; a "local only, nothing leaves this machine except configured model calls" line; and the ask box.
- Each history entry has **"Ask a follow-up"**. It pre-fills the box with an editable standalone question and does not carry over context (see the chat answer below).

**1. Sources.** One card each for Gmail, Granola, Calendar, Slack, and File upload. Each card shows:
- **Connection state:** configured / not configured / sync failing.
- **Last successful sync** and the outcome of the last run.
- **Counts:** live episodes, plus tombstoned ones (with a purge-eligible count).
- **Configured scope:**
  - Gmail: fixed floor 2025-10-04, excluded categories
  - Calendar: owned calendars, 2025-10-04 to now + 3 months
  - Granola: API key present, the key's access scope as reported
  - Slack: scope once decided
- **Connect button, disabled,** labelled "Credentials come from `.env`, see README". Not a dead button.
- **Sync now.** Disabled for unconfigured sources.

The File upload card is a placeholder: "Not yet supported." No ingestion.

**2. Processing log.** This is privacy commitment 5, built as a real screen:
- A run list (newest first): kind, source, config, started, duration, status. Plus counts: inserted / updated / unchanged / tombstoned, cache hits / misses, model calls, tokens in and out, cost.
- A filter by kind and source, and a running cost total for the period (the same numbers as `pkos usage`).
- **Run detail:** each model call (purpose, model, tokens, cost, latency, episode link) and any error, already scrubbed.
- Before step 5 there is nothing persistent to read (U1).

**3. Episodes.** Your main debugging screen, and where citations land.
- **List:** full-text search plus source and date-range filters; title, source, date and participants per row; paginated.
- **Detail:**
  - The body rendered with **section bands**. Model-generated sections, such as `granola_summary`, are visibly labelled "AI-generated by Granola".
  - Participants with roles; refs; meta; a collapsible raw payload.
  - Content hash and first-seen / updated times.
  - `?hl=a-b` scrolls to and highlights a span. This is how citations open.
- **From step 8:** extracted facts and commitments for the current hash, highlighted inline at their spans. Field-ref facts are listed beside the header fields they cite. A version picker covers the coexisting extractor versions (P1), and rejected quotes are listed.
- The step 8 HTML dump reuses this detail template rendered to static files, so there's one view of an extraction, not two.

**4. Answer**
- The question, the configuration that answered (name and config hash), tokens, cost and latency.
- The answer with numbered citations. Each one opens `/episodes/{id}?hl=start-end` at the supporting span.
- **The retrieved set** as a table:
  - rank, source item, chunk or fact, per-mode scores (full text, vector), fused rank;
  - **cited ✓** marked, so retrieved-but-uncited items and citations are visible side by side.
- **Abstention is a designed state**, not an error: "Your archive doesn't support an answer to this." Below that: the best retrieved items, their scores, and the threshold, so you can see whether it was a retrieval miss or a correct abstention.
- If a cited episode has since been deleted upstream, its citation shows "source deleted" and its excerpt is hidden.

**5. Settings**
- **Read-only effective configuration, each value with where it comes from:**
  - model profiles and the pricing table (`configs/llm.toml`);
  - available system configurations;
  - spend cap and purge window (`.env`).
- **Actions:**
  - **Purge now:** the count of eligible tombstones, then a confirmation.
  - **Delete source:** a confirmation page showing exactly what will go (episodes, chunks, facts, commitments, cache entries, history entries citing it). You type the source name to confirm. This is immediate hard deletion, consistent with `pkos delete`.

#### Data added for 4b

```sql
-- 0006_asks.sql (number assigned when built)
CREATE TABLE asks (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    question     text NOT NULL,
    config_name  text NOT NULL,
    config_sha   text NOT NULL,
    answer       text,                      -- NULL when abstained
    abstained    boolean NOT NULL,
    run_id       bigint REFERENCES runs(id) ON DELETE SET NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE ask_items (                     -- the retrieved set, cited or not
    ask_id       bigint NOT NULL REFERENCES asks(id) ON DELETE CASCADE,
    rank         integer NOT NULL,
    episode_id   bigint NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    item_kind    text NOT NULL CHECK (item_kind IN ('chunk','fact','commitment')),
    item_id      bigint NOT NULL,
    span_start   integer, span_end integer,
    scores       jsonb NOT NULL DEFAULT '{}'::jsonb,
    cited_as     integer,                   -- citation number in the answer, NULL if not cited
    PRIMARY KEY (ask_id, rank)
);
CREATE INDEX ask_items_episode_idx ON ask_items (episode_id);
```

**History is derived personal data, so it obeys real deletion.** An answer's text can quote an episode, so whenever an episode is hard-deleted (purge, `pkos delete`, delete source), every `ask` that **cited** it is deleted in the same transaction. Asks that only retrieved it lose that `ask_items` row through the cascade. Tested like every other cascade.

#### Build phasing

| Built in | What |
|---|---|
| **4b** (after step 5) | All six screens over what exists: Ask and Answer with the chunk configurations; history; Episodes without facts; Sources (Granola live, the others shown as not connected); Processing log from `runs`/`model_calls`; Settings read-only plus purge and delete-source; Host/CSRF protection; `asks` tables; demo dataset (U5) |
| 6, 7 | Gmail and Calendar cards go live (scope display, sync now). No new UI code beyond the connector registry |
| 8 | Facts, commitments and rejected quotes inline in episode detail; version picker; the HTML dump reuses the template |
| 9 | Facts configurations in the picker; fact citations in the answer view |
| 10 | Slack card goes live |
| Phase two | Person pages, commitments view, graph explorer, follow-up rewriting (see the chat answer below) |

#### The chat question

**I agree with you, and I'd make one change: build follow-ups as query rewriting, never as conversation context.**

Your diagnosis is right. In multi-turn chat the answer model sees earlier turns, so it can answer from them. An answer can then be right in the chat and unsupported by the store. That's also a system the harness never measures.

**How follow-ups keep correspondence with the harness.** A follow-up is a two-stage pipeline, where only the first stage knows the conversation:
1. **Rewrite.** The previous question, the previous answer and the new turn go in. A **standalone question** comes out ("who else was in that meeting?" becomes "Who attended the 2026-03-02 design review besides Pat?"). It's shown to you, editable, before it runs.
2. **Answer.** The standalone question runs through **exactly** the pipeline the harness scores: fresh retrieval, and an answer model that **never sees prior turns**. Citations are validated against that fresh retrieval only.

The previous answer may steer the rewrite (which meeting, which person). It can only change *what is retrieved*. It cannot become a source, because nothing reaches the answer stage except the standalone question and the retrieved items.

So every answer is `pipeline(standalone_question)`, which is precisely what the eval harness measures. The only new component, the rewriter, gets its own small eval: follow-up pairs, judged on whether the rewrite preserves intent.

**In 4b:** "Ask a follow-up" pre-fills the box and you write the standalone question yourself. That's the manual version of stage 1, with zero correspondence risk. The automatic rewriter is phase two.

#### Design decisions needing your call

- **U1. Build 4b after step 5, not between 4 and 5.**
  - Before step 5, runs are only accounted in memory, inside whichever process ran them: the CLI container, not the web process. So the processing log would have nothing real to show, and would get built twice.
  - Step 5 is small.
  - Alternatively, keep 4b right after 4 and ship the log screen as "available from step 5". I'd rather not ship a placeholder for a privacy commitment.
- **U2. Settings are read-only, except delete and purge.**
  - Editing model profiles, spend caps or the purge window in the UI would create a second source of truth beside `.env` and `configs/`. Writing `.env` from a web process also means a web process writing secrets.
  - "Hosted vs local" becomes the **configuration picker on Ask**, which matches the harness exactly: a configuration is a file, and local vs hosted are configurations.
  - Editable settings can come later if you find yourself wanting them.
- **U3. "Sync now" runs in a background thread inside the web process.**
  - An initial Gmail sync can take many minutes, longer than any request should.
  - The thread writes progress to its `runs` row and the card polls `/sources/{s}/status`.
  - The existing per-source advisory lock stops a UI sync and a CLI sync from interleaving. Sync is already crash-safe, so a web restart mid-sync loses nothing; the next run re-fetches.
  - No queue and no new service. This is the background work you asked me to name.
- **U4. Delete source is synchronous,** inside one transaction with a progress spinner. At phase-one sizes (thousands of episodes) the cascade takes seconds. If it ever doesn't, it moves to the same thread pattern as U3.
- **U5. A synthetic demo dataset for the stranger, and for screenshots.**
  - A stranger who clones the repo has no credentials, so they'd see six empty screens. You've also ruled out personal data in screenshots.
  - `pkos demo` would load a committed synthetic corpus (a fictional person's email, calendar and notes, about 50 episodes) into a separate `pkos_demo` database. The UI can be pointed at it with `PKOS_DB_NAME=pkos_demo`.
  - README screenshots come only from the demo database.
  - Is this in scope for 4b? I think it's what actually makes the 30-second explanation work.

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

### Step 7b: Question set (gate: step 8 does not start until the set is frozen)

Extraction tuning starts in step 8, so the frozen set has to exist before it. Drafting it now also guarantees no extractor output can have shaped it. Full procedure in **§ Question set** below.

- [ ] You write 30–40 questions from memory first, before seeing any drafts (Q-1)
- [ ] Corpus profile: episode counts by source, month, thread size and recurring series; proposed target distribution across 5 classes × 3 sources, for your OK
- [ ] Fixed, seeded, stratified episode sample and episode clusters for drafting; sample manifest saved
- [ ] Drafting run: ~250 candidates (single-episode and multi-episode), plus near-miss unanswerables flagged separately
- [ ] Automatic checks per candidate: the gold episode exists and is live; the answer string is present in, or entailed by, the gold text; lexical overlap with the gold text measured and high-overlap candidates flagged; for near misses, a corpus-wide full-text search for the key terms, with hits shown to you
- [ ] Review file in `data/questions/review.html`: you accept, edit or reject each one
- [ ] Merge to 180 (including your own); split dev/test with a seeded stratified script, not by hand; `pkos eval --freeze`
- [ ] **STOP**: frozen set confirmed

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

## Question set

There's no corpus to write 180 questions against until Gmail and Calendar land, so the set is built in step 7b. Steps 3–4 use a synthetic CSV and a Granola smoke set.

**Drafting rules**
1. Drafted from **raw episode text only**, never from extracted facts. Step 7b runs before extraction exists, so this holds by construction.
2. The drafting model is **neither the answer model nor the judge model**, and preferably a different provider family from both.
3. Every candidate carries: the question, a proposed gold answer, the gold episode id(s), the class, an as-of date when the answer can change over time, and the drafting mode (single-episode, multi-episode, near-miss, or yours).
4. Over-generate about 250 candidates; you cut to 180.
5. **Near-miss unanswerables**: plausible questions about people, projects and dates that appear in the archive, but whose answer is absent. Flagged separately; you check them hardest.
6. The target distribution across 5 classes × 3 sources is proposed from the corpus profile, for your OK.

**Bias mitigations** (see §4 Q-1 to Q-7 for why each exists)
- **Q-1.** You write 30–40 questions from memory before seeing any drafts. They're tagged `origin=user` and reported as their own slice.
- **Q-2.** Relational, temporal and aggregate candidates are drafted from **multi-episode clusters** (a thread, a recurring series, a person over months), and must cite every episode needed.
- **Q-3.** The drafter is told to paraphrase. Lexical overlap between question and gold text is measured and shown in review, and the result tables report accuracy for low- and high-overlap questions separately.
- **Q-4.** The sample is stratified and seeded, and its manifest is saved, so selection is reproducible and not driven by salience.
- **Q-5.** Gold sources mean "sufficient", not "exhaustive": recall@k counts any gold episode. Citation precision is judged per cited episode, so citing a different valid source isn't penalised.
- **Q-6.** Near misses get a corpus-wide full-text search for their key terms, with the hits shown to you during review.
- **Q-7.** The dev/test split is made by a seeded stratified script. The smoke set is excluded.

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

**On the question-set procedure** (your design, plus the biases it would otherwise carry):
- **Q-1. Anchoring and ecological validity (the biggest one).** If every question starts as a model draft, the set measures "what a model thinks is askable from a passage", not what you actually ask. Reviewing 250 plausible drafts anchors you toward accepting them. Your own from-memory questions, written first, are the only part of the set that represents real use, so they're reported as their own slice.
- **Q-2. Single-episode bias works against the thesis.** A drafter reading one episode at a time produces single-hop questions that chunk retrieval answers well. That understates exactly the relational and temporal advantage the project is testing. Multi-episode drafting fixes it.
- **Q-3. Lexical overlap inflates the baseline.** A model writing a question from a passage reuses its rare words, which makes keyword and vector retrieval look better than they would on your own phrasing. That narrows the gap the phase-one gate measures. Paraphrasing plus an overlap metric makes the effect visible instead of hidden.
- **Q-4. Salience selection.** Without a fixed, stratified sample, drafts cluster on memorable threads and skip the mundane bulk your real questions also hit.
- **Q-5. Gold incompleteness and knowledge updates.** One proving episode is rarely the only one. A later email can also change the answer, so "who owns X" drafted from a March email may be wrong by June. Treating gold as sufficient rather than exhaustive, and adding as-of dates, stops valid answers being scored as wrong.
- **Q-6. "Genuinely absent" is hard to verify by memory over 12 months.** The drafter has only seen a sample. A corpus-wide search for the key terms catches near misses that are actually answerable.
- **Q-7. Split leakage.** A hand-made split can drift toward putting easy questions in test. A seeded script can't. The smoke set has been looked at during development, so it's excluded.

One thing that holds by construction: drafting in step 7b happens before any extraction exists, so "the extractor grading its own homework" can't occur. That's also why step 8 is gated on the freeze.

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
| D10 | CSV as proposed plus `obligational` class, `split` and `origin` columns; 180 questions = 150 `test` (frozen, reported) + 30 `dev` (tuning, ≥ 8 unanswerable); built in step 7b per § Question set; sha256 check |
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
| G1 | Granola: the 10 notes (2026-09-21 to 2026-10-02) are the whole archive |
| G2 | Granola extraction reads `transcript` and `my_notes`; `granola_summary` is passed to the prompt as context for naming speakers only, never as a fact source |
| G3 | Granola body layout approved: `my_notes`, `transcript` (merged `Name: text` turns), `granola_summary` (generated) |
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
**Before step 3**: nothing; the harness is built against a synthetic CSV.
**Before step 7b**: your 30–40 from-memory questions, written before you see any drafts; a drafting model choice (different from answer and judge).
**Before step 4**: hosted provider choice, API key, model choice for answer/extract/judge, per-run spend cap; the local model you want in Ollama (I'll suggest candidates that fit 18 GB).
**Before step 6**: Google Cloud OAuth client (Desktop app) with Gmail and Calendar APIs enabled and you as a test user; `client_secret.json` in `data/secrets/`.
**Before step 10**: Slack workspace, whether you can create an app there (or need admin approval), and which conversations are in scope (public channels you're in, private channels, DMs, group DMs) and over what window.
