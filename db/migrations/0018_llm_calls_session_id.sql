-- 0018_llm_calls_session_id — promote session_id from metadata_json to a
-- queryable column for Langfuse/OTel integration and per-agent forensics.
--
-- claude CLI's stream-json output emits session_id on every assistant
-- message (the SDK's conversation key). codex CLI likewise. We've been
-- storing it inside metadata_json which makes JOIN/WHERE expensive
-- (json_extract per row). Promoting to a column makes "show me every
-- LLM call from session X" a one-index lookup.
--
-- Idempotent: ALTER TABLE ADD COLUMN is a no-op on a fresh install if the
-- column already exists, but SQLite errors on re-add — guard with a
-- COLUMN-EXISTS check using PRAGMA. Since SQLite doesn't have IF NOT
-- EXISTS for ADD COLUMN, we use a deferred approach: the db/init.sh
-- migration runner records applied filenames and skips dupes.

ALTER TABLE llm_calls ADD COLUMN session_id TEXT;

CREATE INDEX IF NOT EXISTS idx_llm_calls_session
  ON llm_calls(session_id) WHERE session_id IS NOT NULL;

-- Backfill from existing metadata_json
UPDATE llm_calls
   SET session_id = json_extract(metadata_json, '$.session_id')
 WHERE session_id IS NULL
   AND metadata_json IS NOT NULL;
