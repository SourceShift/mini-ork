-- mini-ork migration 0013 — task_runs (universal task loop runtime)
--
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0013_task_runs.sql
--
-- ## Why this table exists separately from `runs`:
-- `runs` (migration 0001_core.sql) is the libwit-shape lifecycle: epic → run → iter,
-- with epic_id FK + worktree branches. That schema fits the bdd-first-delivery recipe
-- shape and other multi-epic pipelines.
--
-- `task_runs` is the universal-task-loop runtime shape (book Ch 4): each kickoff is
-- a single task that flows classify → plan → execute → verify → reflect → improve.
-- No mandatory epic, no mandatory worktree branch. Each recipe (code-fix, etc.) lands
-- a row here per `mini-ork run <recipe> <kickoff>` invocation.
--
-- The two tables coexist; recipes that need the epic lifecycle (e.g. bdd-first-delivery)
-- may write to BOTH (task_runs as the parent, runs as the per-sub-epic detail).

BEGIN;

CREATE TABLE IF NOT EXISTS task_runs (
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
                    CHECK (status IN ('classified','planned','executing','verifying','reviewing','published','rolled_back','failed')),
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
  notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_runs_task_class ON task_runs(task_class);
CREATE INDEX IF NOT EXISTS idx_task_runs_recipe     ON task_runs(recipe);
CREATE INDEX IF NOT EXISTS idx_task_runs_status     ON task_runs(status);
CREATE INDEX IF NOT EXISTS idx_task_runs_verdict    ON task_runs(verdict) WHERE verdict IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_task_runs_created    ON task_runs(created_at DESC);

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0013_task_runs.sql', strftime('%s','now'), 'phase-a-redesign-osr');

COMMIT;
