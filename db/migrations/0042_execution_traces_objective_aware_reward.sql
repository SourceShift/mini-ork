-- 0042_execution_traces_objective_aware_reward.sql
-- Add objective-aware, direction-normalized reward columns to execution_traces.
--
-- Background: process_reward (0031) is a single 0.0-1.0 scalar that mixes
-- objectives into one bucket. This blocks learning loops for non-code tasks
-- (review, research, ops) which have their own metric shapes (lower-is-better
-- latency, ratio-shaped coverage, dollar-anchored cost). This migration adds:
--
--   objective_domain      TEXT  -- code-delivery | review | research | ops | ...
--   segment               TEXT  -- task_class-shaped sub-bucket for slicing
--   reward_primary_metric TEXT  -- 'latency_ms', 'tests_passed', 'cost_usd', ...
--   reward_direction      TEXT  -- 'higher_is_better' | 'lower_is_better'
--   reward_value          REAL  -- the observed metric value
--   reward_anchor         REAL  -- baseline/expected value to normalize against
--   reward_g              REAL  -- direction-normalized gain: dir*(value-anchor)/abs(anchor)
--   reward_vector_json    TEXT  -- JSON map of secondary metrics
--   reward_source         TEXT  -- 'verifier@v1' | 'human' | 'prm@v1' | ...
--   validity              TEXT  -- 'valid' | 'suspect' | 'invalid'
--
-- Backward compatibility: every new column has a safe default so the existing
-- scalar-reward callers continue to write successfully. Objective_domain defaults
-- to 'code-delivery' and reward_source to 'verifier@v1' (legacy process_reward
-- producer). Reward_direction defaults to 'higher_is_better' which is the
-- natural direction for 0.0-1.0 PRM scores.

PRAGMA foreign_keys = OFF;

ALTER TABLE execution_traces ADD COLUMN objective_domain      TEXT    NOT NULL DEFAULT 'code-delivery';
ALTER TABLE execution_traces ADD COLUMN segment               TEXT    DEFAULT NULL;
ALTER TABLE execution_traces ADD COLUMN reward_primary_metric TEXT    DEFAULT NULL;
ALTER TABLE execution_traces ADD COLUMN reward_direction      TEXT    NOT NULL DEFAULT 'higher_is_better';
ALTER TABLE execution_traces ADD COLUMN reward_value          REAL    DEFAULT NULL;
ALTER TABLE execution_traces ADD COLUMN reward_anchor         REAL    DEFAULT NULL;
ALTER TABLE execution_traces ADD COLUMN reward_g              REAL    DEFAULT NULL;
ALTER TABLE execution_traces ADD COLUMN reward_vector_json    TEXT    DEFAULT NULL;
ALTER TABLE execution_traces ADD COLUMN reward_source         TEXT    NOT NULL DEFAULT 'verifier@v1';
ALTER TABLE execution_traces ADD COLUMN validity              TEXT    NOT NULL DEFAULT 'valid';

CREATE INDEX IF NOT EXISTS idx_et_objective_domain
  ON execution_traces(objective_domain);
CREATE INDEX IF NOT EXISTS idx_et_objective_segment
  ON execution_traces(objective_domain, segment) WHERE segment IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_et_reward_g
  ON execution_traces(reward_g) WHERE reward_g IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_et_reward_source
  ON execution_traces(reward_source);
CREATE INDEX IF NOT EXISTS idx_et_validity
  ON execution_traces(validity) WHERE validity != 'valid';

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0042_execution_traces_objective_aware_reward.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'objective-aware-reward-v1');