-- 0019: execution_traces — widen status CHECK to ('success','failure',
-- 'pending','running','vacuous').
--
-- Two silent-drop bugs behind one CHECK constraint:
--   1. 'running' — bin/mini-ork-execute / -reflect / -verify write a
--      status:"running" trace at phase start; the 0014 CHECK rejects it
--      and the caller's `2>/dev/null || true` swallows the error. The
--      trace only materializes at phase end — in-flight runs are
--      invisible to the obs UI.
--   2. 'vacuous' — mini-ork-verify's minimum-evidence policy produces a
--      verdict=vacuous when zero verifiers execute. Mapping that to
--      status='success' launders "nothing was checked" into a pass;
--      a dedicated status lets the UI render it honestly.
--
-- SQLite can't ALTER a CHECK constraint — full recreate dance (same as 0014).

PRAGMA foreign_keys=off;

CREATE TABLE execution_traces_new (
  trace_id              TEXT    PRIMARY KEY,
  run_id                INTEGER REFERENCES runs(id) ON DELETE CASCADE,
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
                        CHECK (status IN ('success','failure','pending','running','vacuous')),
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

CREATE INDEX IF NOT EXISTS idx_et_run_id_v19            ON execution_traces(run_id);
CREATE INDEX IF NOT EXISTS idx_et_task_class_v19        ON execution_traces(task_class);
CREATE INDEX IF NOT EXISTS idx_et_workflow_version_v19  ON execution_traces(workflow_version_id);
CREATE INDEX IF NOT EXISTS idx_et_agent_version_v19     ON execution_traces(agent_version_id);
CREATE INDEX IF NOT EXISTS idx_et_status_v19            ON execution_traces(status);
CREATE INDEX IF NOT EXISTS idx_et_created_v19           ON execution_traces(created_at DESC);

PRAGMA foreign_keys=on;
