-- 0066_llm_calls_thinking_tokens.sql - record reasoning ("thinking") tokens per call.
--
-- Claude-CLI envelopes report usage.output_tokens_details.thinking_tokens
-- (a breakdown of output_tokens, never additive to it). Without a column the
-- schema-adaptive writers dropped it, so consumers (e.g. the researcher cost
-- ledger) recorded 0 thinking tokens for every mini-ork call. Existing rows
-- default to 0 (not measured).

BEGIN;

ALTER TABLE llm_calls
  ADD COLUMN thinking_tokens INTEGER DEFAULT 0;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0066_llm_calls_thinking_tokens.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), '');

COMMIT;
