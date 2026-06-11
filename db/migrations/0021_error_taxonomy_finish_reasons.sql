-- 0021_error_taxonomy_finish_reasons.sql — classify LLM failures and node exits.
--
-- NULL means "legacy/pre-migration row". New dispatches populate these fields
-- when the migrated columns are available.

BEGIN;

ALTER TABLE llm_calls
  ADD COLUMN error_category TEXT NULL
  CHECK (
    error_category IS NULL OR error_category IN (
      'auth',
      'quota',
      'capacity',
      'request',
      'safety',
      'network',
      'stream',
      'provider',
      'config',
      'unknown'
    )
  );

ALTER TABLE llm_calls
  ADD COLUMN retryable INTEGER NULL
  CHECK (retryable IS NULL OR retryable IN (0, 1));

ALTER TABLE run_events
  ADD COLUMN finish_reason TEXT NULL
  CHECK (
    finish_reason IS NULL OR finish_reason IN (
      'done',
      'error',
      'interrupted',
      'max_steps',
      'cost_limit',
      'timeout',
      'verdict_revise',
      'verdict_fail'
    )
  );

CREATE INDEX IF NOT EXISTS idx_llm_calls_error_category
  ON llm_calls(error_category)
  WHERE error_category IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_run_events_node_end_finish_reason
  ON run_events(finish_reason, created_at)
  WHERE event_type = 'node_end' AND finish_reason IS NOT NULL;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0021_error_taxonomy_finish_reasons.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), '');

COMMIT;
