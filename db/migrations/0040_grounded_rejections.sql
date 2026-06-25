-- 0040_grounded_rejections.sql
-- Phase 0 learning-loop proof surface for grounded/refuted draft events.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS grounded_rejections (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id            TEXT,
  trace_id          TEXT,
  task_class        TEXT,
  node_type         TEXT,
  claim             TEXT NOT NULL,
  refutation        TEXT NOT NULL,
  evidence_json     TEXT NOT NULL DEFAULT '[]',
  source_artifact   TEXT,
  created_at        INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_grounded_rejections_run
  ON grounded_rejections(run_id);

CREATE INDEX IF NOT EXISTS idx_grounded_rejections_task_class
  ON grounded_rejections(task_class, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_grounded_rejections_trace
  ON grounded_rejections(trace_id)
  WHERE trace_id IS NOT NULL;

PRAGMA foreign_keys = ON;
