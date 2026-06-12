-- 0024_cache_aware_cost.sql - cache-aware llm_calls token and cost telemetry.
--
-- Existing rows default to zero cache activity. cost_usd remains the provider
-- envelope total; the component columns make input/cache billing inspectable.

BEGIN;

ALTER TABLE llm_calls
  ADD COLUMN cached_input_tokens INTEGER DEFAULT 0;

ALTER TABLE llm_calls
  ADD COLUMN cache_creation_input_tokens INTEGER DEFAULT 0;

ALTER TABLE llm_calls
  ADD COLUMN cost_input_uncached_usd REAL DEFAULT 0;

ALTER TABLE llm_calls
  ADD COLUMN cost_input_cached_usd REAL DEFAULT 0;

ALTER TABLE llm_calls
  ADD COLUMN cost_cache_write_usd REAL DEFAULT 0;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0024_cache_aware_cost.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), '');

COMMIT;
