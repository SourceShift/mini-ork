-- 0062_learning_ledger.sql
-- learning-memory P1a: D5 lesson-injection ledger + D6 pass-stats.
-- Kickoff: docs/plans/2026-10-07-learning-memory-page-refactor.md (P1a),
-- kickoffs/auto/learn-ledger.md.
--
-- Two write-only tables behind the learning pipeline:
--
--   lesson_injections   -- one row per (run, node, attempt, source) actually
--                          injected into an LLM prompt. The IDE "Lesson
--                          uses" counter and the future holdout evaluation
--                          read this table.
--   learning_pass_stats -- one row per pass execution (a pass is one stage
--                          of the learning pipeline, e.g. pattern_induction,
--                          reflection, etc.). Drives the IDE health strip.
--
-- Both are ADDITIVE. Nothing reads them yet -- the IDE-exposure phase (P1b)
-- wires the routes; nothing writes held_out=1 yet (P4 holdout eval).
--
-- Concurrency: same single-writer model as the rest of the control-plane DB.
-- The ledger writer (mini_ork/learning/ledger.py) opens with
-- PRAGMA busy_timeout=5000, mirroring pattern_induction.py:753-754.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS lesson_injections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL,
    node_id      TEXT    NOT NULL,
    node_type    TEXT,
    lane         TEXT,
    task_class   TEXT,
    attempt      INTEGER,
    source_kind  TEXT    NOT NULL,    -- gradient | pattern | steering
    source_id    TEXT    NOT NULL,
    held_out     INTEGER NOT NULL DEFAULT 0,   -- reserved for holdout eval
    ts           INTEGER NOT NULL
);

-- The dedup key: a retried write must not double-count. SQLite supports
-- uniqueness on a non-PK column set only as a UNIQUE INDEX, not as a table
-- constraint -- matches the 0061_remote_procs pattern.
CREATE UNIQUE INDEX IF NOT EXISTS idx_lesson_injections_unique
  ON lesson_injections(run_id, node_id, attempt, source_kind, source_id, held_out);

-- Hot read: "how many times has this lesson been used?"
CREATE INDEX IF NOT EXISTS idx_lesson_injections_source
  ON lesson_injections(source_kind, source_id);

-- Hot read: "what did this run inject, in order?"
CREATE INDEX IF NOT EXISTS idx_lesson_injections_run_node
  ON lesson_injections(run_id, node_id);

CREATE TABLE IF NOT EXISTS learning_pass_stats (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_id    TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    stage      TEXT    NOT NULL,    -- pattern_induction | reflection | ...
    inputs     INTEGER,
    outputs    INTEGER,
    failures   INTEGER,
    lane       TEXT,
    last_error TEXT
);

-- Hot read: stage_health() walks the last N rows for each stage.
CREATE INDEX IF NOT EXISTS idx_learning_pass_stats_stage_ts
  ON learning_pass_stats(stage, ts);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0062_learning_ledger.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'learning-ledger-v1');
