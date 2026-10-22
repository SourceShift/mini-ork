-- 0080: give a run delivered some other way a terminal state of its own.
--
-- A failed run whose work was delivered elsewhere (a later run of the same
-- kickoff published, its diff landed on origin/main, or its epic closed `done`
-- by another route) kept sitting in the failed bucket and stayed eligible for
-- repair sweeps (gap G1, docs/todos/2026-10-15-failed-runs-4d-revive-plan.md).
-- `superseded` is that fact as a status: TERMINAL (nothing further is coming)
-- but NOT unsuccessful -- the work shipped, so auto-repair and the failure
-- banners must not chase it -- and it buckets with `not_needed` on the board.
-- mini_ork/run_status.py is the one place that split is defined.
--
-- Widening a CHECK constraint needs a table rebuild, exactly the shape of
-- 0076_task_runs_status_not_needed.sql. The four ALTER-added columns are named
-- unconditionally: on every install -- fresh or upgraded -- 0022
-- (dispatch_config_json, agents_yaml_sha) and 0023 (fuse_blown_lane,
-- fuse_consecutive_failures) have already run, so there is no earlier path to
-- guard for.
--
-- foreign_keys is turned off BEFORE the transaction, because a PRAGMA inside a
-- transaction is a no-op: with enforcement on, DROP TABLE task_runs cascades
-- into run_spawns' parent_run_id rows (ON DELETE CASCADE, 0016). The PRAGMA has
-- to sit outside BEGIN, which is also why this file carries its own BEGIN --
-- stores/migrate.py only wraps a file that does not manage its transaction.

PRAGMA foreign_keys=off;

BEGIN;

CREATE TABLE task_runs_new (
  id                TEXT PRIMARY KEY,                 -- run-<unix-ts>-<pid> or user-supplied
  task_class        TEXT NOT NULL,                    -- 'code_fix', 'bdd_first_delivery', 'research_synthesis', etc.
  recipe            TEXT,                             -- 'code-fix', 'bdd-first-delivery', or NULL if no recipe
  workflow_version  TEXT NOT NULL DEFAULT 'latest',
  kickoff_path      TEXT NOT NULL,
  plan_path         TEXT,
  plan_hash         TEXT,                              -- sha256 first 16 hex of plan JSON
  artifact_path     TEXT,
  artifact_hash     TEXT,
  status            TEXT NOT NULL DEFAULT 'classified'
                    CHECK (status IN ('classified','planned','executing','verifying','reviewing','published','rolled_back','failed','not_needed','superseded')),
  verdict           TEXT
                    CHECK (verdict IN ('APPROVE','REQUEST_CHANGES','ESCALATE','CRASH') OR verdict IS NULL),
  cost_usd          REAL NOT NULL DEFAULT 0,
  duration_ms       INTEGER NOT NULL DEFAULT 0,
  trace_id          TEXT,                              -- root trace_id for the run; child traces FK back
  created_at        INTEGER NOT NULL,                  -- unix timestamp
  updated_at        INTEGER NOT NULL,
  ended_at          INTEGER,
  -- optional cross-table linkage when a recipe spans multiple sub-epics
  parent_epic_id    TEXT,
  notes             TEXT,
  dispatch_config_json TEXT NULL,
  agents_yaml_sha TEXT NULL,
  fuse_blown_lane TEXT NULL,
  fuse_consecutive_failures INTEGER NOT NULL DEFAULT 0
);

INSERT INTO task_runs_new (
  id, task_class, recipe, workflow_version, kickoff_path, plan_path, plan_hash,
  artifact_path, artifact_hash, status, verdict, cost_usd, duration_ms,
  trace_id, created_at, updated_at, ended_at, parent_epic_id, notes,
  dispatch_config_json, agents_yaml_sha, fuse_blown_lane, fuse_consecutive_failures
)
SELECT
  id, task_class, recipe, workflow_version, kickoff_path, plan_path, plan_hash,
  artifact_path, artifact_hash, status, verdict, cost_usd, duration_ms,
  trace_id, created_at, updated_at, ended_at, parent_epic_id, notes,
  dispatch_config_json, agents_yaml_sha, fuse_blown_lane, fuse_consecutive_failures
FROM task_runs;

DROP TABLE task_runs;
ALTER TABLE task_runs_new RENAME TO task_runs;

CREATE INDEX idx_task_runs_task_class ON task_runs(task_class);
CREATE INDEX idx_task_runs_recipe     ON task_runs(recipe);
CREATE INDEX idx_task_runs_status     ON task_runs(status);
CREATE INDEX idx_task_runs_verdict    ON task_runs(verdict) WHERE verdict IS NOT NULL;
CREATE INDEX idx_task_runs_created    ON task_runs(created_at DESC);
CREATE INDEX idx_task_runs_agents_yaml_sha
  ON task_runs(agents_yaml_sha)
  WHERE agents_yaml_sha IS NOT NULL;
CREATE INDEX idx_task_runs_fuse_blown_lane
  ON task_runs(fuse_blown_lane)
  WHERE fuse_blown_lane IS NOT NULL;

COMMIT;

PRAGMA foreign_keys=on;
