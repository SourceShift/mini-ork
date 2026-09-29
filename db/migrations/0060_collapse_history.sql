-- 0060_collapse_history.sql
-- collapse_halt signal source-of-truth (kickoff auto/rsi-i4-collapse-halt-repair.md):
-- G7 collapse_detector.detect() needs a promotion history the breaker can read
-- directly. The detector is a pure function over [{step, score, anchor,
-- directives}, ...]; this table persists those rows so a production trip does
-- not depend on a caller forward-passing a kwarg (which the prior pass did,
-- and which the prior review correctly flagged as inert — `collapse_history is
-- None` → empty list → detector's n<MIN_STEPS branch → "none" → no trip, in
-- every production call).
--
-- ADditive only. No existing column is altered / dropped / backfilled; the
-- collapse detector (mini_ork/learning/collapse_detector.py) reads a list the
-- caller assembles, so this table is consumed by mini_ork/recovery/
-- circuit_breaker.py:281 which now SELECTs rows by task_class when no kwarg
-- was injected.
--
-- Why per-(task_class, step) index: every read is "give me the history for
-- task_class X ordered by step" — that is the only access pattern. Without
-- the composite index the SELECT degenerates to a table scan as the table
-- grows, and the production trip path adds latency on the liveness gate.
--
-- Idempotency: brand-new table, so CREATE TABLE IF NOT EXISTS +
-- CREATE INDEX IF NOT EXISTS is sufficient. INSERT OR IGNORE INTO
-- schema_migrations at the bottom keeps `mini-ork init` re-runs stable
-- (matches 0047_run_artifacts.sql:52-54 / 0058_semantic_memory_attribution.sql:43-46).

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS collapse_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_class  TEXT    NOT NULL,
    step        INTEGER NOT NULL,
    score       REAL    NOT NULL,
    anchor      REAL    NOT NULL,
    directives  INTEGER NOT NULL DEFAULT 0,   -- count, not list (the detector
                                              -- coerces 0 → [] via the
                                              -- breaker's row mapper)
    run_id      TEXT,                          -- nullable: writer may not have
                                              -- the run_id available at flush
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_collapse_history_tc_step
  ON collapse_history(task_class, step);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0060_collapse_history.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'collapse-history-v1');