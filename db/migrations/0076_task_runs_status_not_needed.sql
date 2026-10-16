-- 0076: give an operator-closed run a terminal state of its own.
--
-- `board dismiss <run>` recorded "I do not need this run" as a per-run file
-- marker read by one page (ide_pages/inbox.py); the row itself stayed whatever
-- it was, `failed` in practice, so the run counted against the failed bucket
-- forever and the reaper kept selecting it. `not_needed` is that gesture as a
-- status: TERMINAL (nothing further is coming, so nothing should wait on it)
-- but NOT unsuccessful (auto-repair, retry-hint and the reward write-back must
-- not chase a run the operator closed on purpose). mini_ork/run_status.py is
-- the one place that split is defined.
--
-- Widening a CHECK constraint needs a table rebuild. The four ALTER-added
-- columns are named unconditionally because 0076 is the last migration, so on
-- every install -- fresh or upgraded -- 0022 (dispatch_config_json,
-- agents_yaml_sha) and 0023 (fuse_blown_lane, fuse_consecutive_failures) have
-- already run; there is no earlier path to guard for.
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
                    CHECK (status IN ('classified','planned','executing','verifying','reviewing','published','rolled_back','failed','not_needed')),
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
