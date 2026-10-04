-- 0002_episodes: per-source sync watermarks and the episode store.
-- Identity is (source, external_id); content_hash is indexed but deliberately not unique (D4).
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
