-- 0071_node_completion_checkpoints.sql
-- P1.2 of the spec-driven-delivery kickoff: a first-class
-- ``mini_ork/checkpoints/`` module that records per-node completion on the
-- dispatch boundary, so a replay restores succeeded nodes' outputs (LangGraph
-- ``_reapply_writes_to_succeeded_nodes`` analogue) and re-dispatches
-- failed / ERROR-marked nodes (F2 / F3 / F4 in /private/tmp/miniork-p1-specs/
-- p1-2-checkpointer.md).
--
-- Distinct from the E1 ``node_checkpoints`` table introduced by
-- 0050_node_dag_checkpoints.sql: that table is the durable-DAG "is this
-- node's work reusable?" ledger keyed by ``(run_id, node_id)`` with input /
-- recipe / config hashes and an artifact manifest. This table is the
-- lighter LangGraph-flavoured "did this step complete, and what was its
-- terminal artifact?" ledger keyed by ``(run_id, step_id)``, written on
-- every node completion (success, failure, or ERROR), and intentionally
-- without input / recipe / config hashing. Both tables coexist; the
-- durable-DAG validity check still wins on reuse.
--
-- AC1 contract (F1 / F5 / F6 in the SpecCard):
--   F1  per-row fields:  run_id, step_id, node_id, status, artifact_path,
--                       artifact_sha256, rc, started_at, ended_at
--   F5  keying:         (run_id, step_id) is the lookup key
--   F6  queryability:   indexable by run_id and by (run_id, step_id)
--
-- Failure modes (Errors section of the SpecCard):
--   - corrupt or missing checkpoint records on resume MUST hard-error
--     naming the node and run, never silently re-execute. The reader is
--     mini_ork/checkpoints/__init__.py::filter_resume_set, which raises
--     CheckpointCorrupt on a row that cannot be parsed; the SQL itself
--     only enforces the F1 shape.
--
-- C2 contract: this migration is ADDITIVE — no ALTERs on existing schema,
-- no rows added to existing tables, no behavior change for any existing
-- recipe. Existing durability / dispatch / run-recovery paths keep their
-- own ledger tables and read no new table.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS node_completion_checkpoints (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,  -- row id for stable ordering
  run_id            TEXT    NOT NULL,                  -- == thread_id (LangGraph analogue)
  step_id           TEXT    NOT NULL,                  -- == checkpoint_id; usually == node_id
  node_id           TEXT    NOT NULL,                  -- denormalised for query convenience
  status            TEXT    NOT NULL
                      CHECK (status IN ('succeeded','failed','error')),
                                                        -- terminal status only; mirror of
                                                        -- LangGraph node terminal vocabulary
  artifact_path     TEXT,                              -- single primary artifact (relative to
                                                        -- MINI_ORK_RUN_DIR; nullable when the
                                                        -- node produced no on-disk output)
  artifact_sha256   TEXT,                              -- sha256 of the bytes at artifact_path;
                                                        -- populated iff artifact_path is non-null
  rc                INTEGER,                           -- process return code; nullable for
                                                        -- non-process node types
  started_at        INTEGER NOT NULL,                   -- unix epoch seconds
  ended_at          INTEGER NOT NULL,                   -- unix epoch seconds
  recorded_at       INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  UNIQUE (run_id, step_id)                             -- F5: at most one completion per (run, step)
);

-- F6 — "queryable by run_id": the hot read for "which steps completed in
-- this run?" (the dispatch boundary consults this on every replay).
CREATE INDEX IF NOT EXISTS idx_node_completion_checkpoints_run
  ON node_completion_checkpoints(run_id);

-- Defence-in-depth (the UNIQUE constraint already enforces the (run_id,
-- step_id) lookup, which is the PK of the resume scan). Listed so the
-- schema matches the F5 / F6 language verbatim.

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0071_node_completion_checkpoints.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'node-completion-checkpoints-v1');