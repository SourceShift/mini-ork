-- 0014: execution_traces — relax run_id FK NOT NULL + widen status check.
--
-- v0.2-pt11 (D-039): close the silent-write bug that made execution_traces
-- EMPTY across all 10+ DF cycles since the framework's inception.
--
-- Root cause: migration 0010 declared
--   run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE
-- but trace_write is called from contexts that don't have a runs.id —
-- single-recipe direct runs use task_runs (a separate table), not runs.
-- Every trace_write INSERT fails NOT NULL constraint and the trace_id row
-- is silently rejected (caller uses `2>/dev/null || true`).
--
-- Compounded by: trace_store.sh's inline CREATE TABLE IF NOT EXISTS used
-- different column names + types (prompt_version vs prompt_version_hash,
-- created_at INTEGER vs TEXT). The IF NOT EXISTS no-op'd because the
-- table existed from 0010, but the INSERT statement targets wrong columns.
--
-- Fix: drop the FK NOT NULL on run_id (still references runs FOR ENFORCEMENT
-- when populated, just nullable when not); widen status check to include
-- 'pending' (trace_store.sh emits this transition state).
--
-- SQLite ALTER TABLE can't drop NOT NULL directly — full recreate dance.

PRAGMA foreign_keys=off;

CREATE TABLE execution_traces_new (
  trace_id              TEXT    PRIMARY KEY,
  run_id                INTEGER REFERENCES runs(id) ON DELETE CASCADE,  -- now nullable
  workflow_version_id   TEXT    REFERENCES workflow_memory(workflow_version_id),
  agent_version_id      TEXT    NOT NULL DEFAULT '',
  task_class            TEXT    NOT NULL,
  prompt_version_hash   TEXT    NOT NULL DEFAULT '',
  context_bundle_hash   TEXT    NOT NULL DEFAULT '',
  tool_calls            TEXT    NOT NULL DEFAULT '[]',
  files_read            TEXT    NOT NULL DEFAULT '[]',
  files_written         TEXT    NOT NULL DEFAULT '[]',
  verifier_output       TEXT    NOT NULL DEFAULT '{}',
  reviewer_verdict      TEXT,
  cost_usd              REAL    NOT NULL DEFAULT 0.0,
  duration_ms           INTEGER NOT NULL DEFAULT 0,
  final_artifact_ref    TEXT,
  status                TEXT    NOT NULL
                        CHECK (status IN ('success','failure','pending')),
  created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

INSERT INTO execution_traces_new
  SELECT trace_id, run_id, workflow_version_id, agent_version_id, task_class,
         prompt_version_hash, context_bundle_hash, tool_calls, files_read,
         files_written, verifier_output, reviewer_verdict, cost_usd,
         duration_ms, final_artifact_ref, status, created_at
  FROM execution_traces;

DROP TABLE execution_traces;
ALTER TABLE execution_traces_new RENAME TO execution_traces;

CREATE INDEX IF NOT EXISTS idx_et_run_id_v14            ON execution_traces(run_id);
CREATE INDEX IF NOT EXISTS idx_et_task_class_v14        ON execution_traces(task_class);
CREATE INDEX IF NOT EXISTS idx_et_workflow_version_v14  ON execution_traces(workflow_version_id);
CREATE INDEX IF NOT EXISTS idx_et_agent_version_v14     ON execution_traces(agent_version_id);
CREATE INDEX IF NOT EXISTS idx_et_status_v14            ON execution_traces(status);
CREATE INDEX IF NOT EXISTS idx_et_created_v14           ON execution_traces(created_at DESC);

PRAGMA foreign_keys=on;

INSERT INTO schema_migrations (filename, applied_at, checksum)
  VALUES ('0014_execution_traces_relax_fk_and_status.sql',
          strftime('%Y-%m-%dT%H:%M:%fZ','now'),
          'pt11-relax-fk-widen-status');
