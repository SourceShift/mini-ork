-- 0092_lesson_outcome_exposures.sql
-- Per-lesson node outcomes: fold `lesson_injections` into `node_attempts`.
--
-- `lesson_injections` (0062) records which lesson every node was given, and
-- which arm it was in. `node_attempts` (0050) records how every attempt ended.
-- The two join on (run_id, node_id, attempt = attempt_no) and nothing has ever
-- joined them: every existing utility tally (`context_item_utility`,
-- `semantic_memory_uses`) resolves at RUN granularity, so one published run
-- credits every lesson shown to any node. This table is the node-level fact,
-- one row per (run, node, lesson, arm).
--
--   lesson_outcome_exposures -- the exposure fact. `arm` is 'delivered' or
--                               'held_out'; `stable` is 0 for the positional
--                               context_v2 `c:<n>` ids, which are kickoff
--                               constraints rather than lessons and are
--                               excluded from every rate.
--   lesson_outcome_runs      -- the marker that lets the resolver be
--                               idempotent (the `context_item_runs` pattern in
--                               mini_ork/learning/item_utility.py).
--
-- Two deliberate non-decisions, both recorded so the next reader does not
-- "fix" them:
--   * The population control is NOT stored. A lesson's control pool changes
--     every time any run resolves, so folding it incrementally would be
--     unsound; it is computed live at read time.
--   * `cost_usd` stays NULL when no attempt carried a cost. It is NULL for
--     75-78% of researcher/implementer/reviewer attempts, and a 0.0 would read
--     as "free" instead of "unknown".
--
-- Additive only: no ALTER, so no guarded-.read idiom is needed, and reapplying
-- this file is a no-op.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS lesson_outcome_exposures (
  run_id      TEXT    NOT NULL,
  node_id     TEXT    NOT NULL,
  source_kind TEXT    NOT NULL,
  source_id   TEXT    NOT NULL,
  arm         TEXT    NOT NULL CHECK (arm IN ('delivered','held_out')),
  stable      INTEGER NOT NULL DEFAULT 1 CHECK (stable IN (0,1)),
  node_type   TEXT    NOT NULL DEFAULT '',
  task_class  TEXT    NOT NULL DEFAULT '',
  outcome_day INTEGER NOT NULL DEFAULT 0,
  result_1    TEXT,
  clean       INTEGER NOT NULL DEFAULT 0 CHECK (clean IN (0,1)),
  retried     INTEGER NOT NULL DEFAULT 0 CHECK (retried IN (0,1)),
  failed      INTEGER NOT NULL DEFAULT 0 CHECK (failed IN (0,1)),
  attempts    INTEGER NOT NULL DEFAULT 0,
  cost_usd    REAL,
  resolved_at INTEGER NOT NULL,
  PRIMARY KEY (run_id, node_id, source_kind, source_id, arm)
);

-- The report's hot read: one lesson's exposures, split by arm.
CREATE INDEX IF NOT EXISTS idx_lesson_outcome_exposures_lesson
  ON lesson_outcome_exposures(source_kind, source_id, arm);

-- The population control's window scan, and the recency rollup.
CREATE INDEX IF NOT EXISTS idx_lesson_outcome_exposures_day
  ON lesson_outcome_exposures(outcome_day DESC);

-- The stratification key: same node type, same task class, same window.
CREATE INDEX IF NOT EXISTS idx_lesson_outcome_exposures_stratum
  ON lesson_outcome_exposures(node_type, task_class, outcome_day);

CREATE TABLE IF NOT EXISTS lesson_outcome_runs (
  run_id      TEXT PRIMARY KEY,
  resolved_at INTEGER NOT NULL,
  note        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_lesson_outcome_runs_resolved
  ON lesson_outcome_runs(resolved_at DESC);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0092_lesson_outcome_exposures.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'lesson-outcome-exposures-v1');
