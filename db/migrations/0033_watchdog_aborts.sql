-- 0033_watchdog_aborts.sql
-- Track B item 5 — Online Auditing for Early Failure Prediction
-- (arXiv:2605.08715). When a running task_run matches a known-failing
-- pattern_records cluster with sufficient match score, the watchdog
-- writes .stop-requested so bin/mini-ork-execute halts at the next
-- node boundary. This table logs every abort decision for audit and
-- post-hoc verification (false-positive rate, true-positive rate).

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS watchdog_aborts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id          TEXT NOT NULL,
  task_class      TEXT,
  matched_pattern TEXT NOT NULL,             -- pattern_records.pattern_id
  match_score     REAL NOT NULL,             -- 0.0-1.0, how confident the match
  evidence        TEXT,                      -- JSON: nodes observed so far + reason
  outcome         TEXT NOT NULL DEFAULT 'aborted'
                    CHECK (outcome IN ('aborted','warned_only','dismissed_by_user')),
  aborted_at      INTEGER NOT NULL,
  -- post-hoc verification: did the abort prevent a real failure?
  verified_failure_avoided TEXT
                    CHECK (verified_failure_avoided IN ('true','false','unknown') OR
                           verified_failure_avoided IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_watchdog_aborts_run
  ON watchdog_aborts(run_id);
CREATE INDEX IF NOT EXISTS idx_watchdog_aborts_pattern
  ON watchdog_aborts(matched_pattern);

PRAGMA foreign_keys = ON;
