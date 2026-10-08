-- 0067_llm_calls_thinking_tokens_nullable.sql — stop defaulting thinking
-- tokens to 0 when the provider never reported them.
--
-- 0066 added ``thinking_tokens INTEGER DEFAULT 0``. That default is a lie: it
-- is indistinguishable from "the model did no thinking". The dispatch writers
-- always supply the column, so the default never fired for them — but a future
-- writer that omits it would silently reintroduce the bug, and the column's own
-- default advertises a 0 that was never measured. The absence must be NULL.
--
-- SQLite cannot change a column DEFAULT in place, so the table is rebuilt:
-- create-new → copy rows → drop-old → rename (the 0065 pattern). The rebuild
-- drops ``thinking_tokens``'s DEFAULT while keeping every other column, its
-- CHECK constraints, and the AUTOINCREMENT primary key byte-for-byte. The whole
-- rebuild is one explicit transaction (the runner honours a migration that
-- manages its own BEGIN — mini_ork/stores/migrate.py:387-390).
--
-- llm_calls has NO dependent view or trigger (verified: nothing in
-- sqlite_master references it), so unlike 0065 there is no view to drop and
-- recreate. DROP TABLE takes the table's indexes with it, so they are
-- recreated below; ``ALTER TABLE … RENAME`` would otherwise re-parse every view
-- in the schema on SQLite >= 3.45 and abort on a dangling reference, which is
-- why 0065's dependent view is handled explicitly — here there is none.

BEGIN;

CREATE TABLE llm_calls_new (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  provider        TEXT NOT NULL,
  model_id        TEXT NOT NULL,
  tier            TEXT NOT NULL,
  feature_name    TEXT NOT NULL,
  actor           TEXT,
  epic_id         TEXT,
  dispatch_id     INTEGER,
  run_id          INTEGER,
  iter            INTEGER,
  input_tokens    INTEGER NOT NULL DEFAULT 0,
  output_tokens   INTEGER NOT NULL DEFAULT 0,
  total_tokens    INTEGER NOT NULL DEFAULT 0,
  cost_usd        REAL NOT NULL DEFAULT 0,
  duration_ms     INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL CHECK (status IN ('success','failed')),
  finish_reason   TEXT,
  error_message   TEXT,
  traceparent     TEXT,
  metadata_json   TEXT NOT NULL DEFAULT '{}',
  ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  session_id      TEXT,
  error_category  TEXT
                  CHECK (error_category IS NULL OR error_category IN (
                    'auth','quota','capacity','request','safety','network',
                    'stream','provider','config','unknown')),
  retryable       INTEGER CHECK (retryable IS NULL OR retryable IN (0, 1)),
  cached_input_tokens         INTEGER DEFAULT 0,
  cache_creation_input_tokens INTEGER DEFAULT 0,
  cost_input_uncached_usd     REAL DEFAULT 0,
  cost_input_cached_usd       REAL DEFAULT 0,
  cost_cache_write_usd        REAL DEFAULT 0,
  -- No DEFAULT: NULL means the provider reported no figure.
  thinking_tokens             INTEGER
);

INSERT INTO llm_calls_new
  (id, provider, model_id, tier, feature_name, actor, epic_id, dispatch_id, run_id,
   iter, input_tokens, output_tokens, total_tokens, cost_usd, duration_ms, status,
   finish_reason, error_message, traceparent, metadata_json, ts, session_id,
   error_category, retryable, cached_input_tokens, cache_creation_input_tokens,
   cost_input_uncached_usd, cost_input_cached_usd, cost_cache_write_usd, thinking_tokens)
SELECT
   id, provider, model_id, tier, feature_name, actor, epic_id, dispatch_id, run_id,
   iter, input_tokens, output_tokens, total_tokens, cost_usd, duration_ms, status,
   finish_reason, error_message, traceparent, metadata_json, ts, session_id,
   error_category, retryable, cached_input_tokens, cache_creation_input_tokens,
   cost_input_uncached_usd, cost_input_cached_usd, cost_cache_write_usd, thinking_tokens
FROM llm_calls;

DROP TABLE llm_calls;

ALTER TABLE llm_calls_new RENAME TO llm_calls;

CREATE INDEX IF NOT EXISTS idx_llm_calls_provider_model ON llm_calls(provider, model_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_feature        ON llm_calls(feature_name);
CREATE INDEX IF NOT EXISTS idx_llm_calls_ts             ON llm_calls(ts);
CREATE INDEX IF NOT EXISTS idx_llm_calls_actor          ON llm_calls(actor) WHERE actor IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_calls_epic           ON llm_calls(epic_id) WHERE epic_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_calls_run            ON llm_calls(run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_calls_session        ON llm_calls(session_id) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_calls_error_category ON llm_calls(error_category) WHERE error_category IS NOT NULL;

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0067_llm_calls_thinking_tokens_nullable.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), '');
