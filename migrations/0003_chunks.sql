-- 0003_chunks: retrieval units over raw episodes (the baseline corpus).
-- kind='body': a slice of body with real offsets.
-- kind='meta': one per episode, rendered from title/time/participants/location so that a calendar
--              event with no description, or an email's sender, is findable at all. It lives here,
--              never in body, so it does not affect content_hash or spans. See PLAN.md X1.
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
