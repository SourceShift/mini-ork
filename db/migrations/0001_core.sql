-- mini-ork migration 0001 — core lifecycle tables
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0001_core.sql
BEGIN;

-- ── schema_migrations ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  checksum   TEXT
);

-- ── epics ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS epics (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  status        TEXT NOT NULL
                CHECK (status IN ('not started','in progress','in review','done','blocked','escalated')),
  lane          TEXT,
  worker_default TEXT,
  reviewer      TEXT DEFAULT 'sonnet',
  group_id      TEXT,
  kickoff_path  TEXT,
  estimated_days REAL,
  notes         TEXT,
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  archived_at   TEXT,
  primary_journey_id TEXT,                          -- logical FK to journeys(id)
  epic_kind     TEXT
                CHECK (epic_kind IN ('fe','be','llm','data','sandbox','doc','mixed') OR epic_kind IS NULL),
  salvage_attempts INTEGER NOT NULL DEFAULT 0,
  last_conflict_kind TEXT
                CHECK (last_conflict_kind IN ('preflight','foreign-writes','tier2-failed','tier3-failed','squash-failed') OR last_conflict_kind IS NULL),
  last_conflict_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_epics_status ON epics(status) WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_epics_lane   ON epics(lane)   WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_epics_group  ON epics(group_id);
CREATE INDEX IF NOT EXISTS idx_epics_kind   ON epics(epic_kind) WHERE epic_kind IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_epics_salvage_attempts
  ON epics(salvage_attempts) WHERE salvage_attempts > 0;

CREATE TRIGGER IF NOT EXISTS trg_epics_updated
AFTER UPDATE ON epics
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE epics SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_epics_no_revert_done
BEFORE UPDATE OF status ON epics
WHEN OLD.status = 'done' AND NEW.status != 'done'
BEGIN
  SELECT RAISE(ABORT, 'epic done→non-done revert blocked by trg_epics_no_revert_done');
END;

CREATE TRIGGER IF NOT EXISTS trg_epics_no_delete_done
BEFORE DELETE ON epics
WHEN OLD.status = 'done' AND OLD.archived_at IS NULL
BEGIN
  SELECT RAISE(ABORT, 'cannot DELETE done epic without first setting archived_at');
END;

-- ── deps ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS deps (
  epic_id     TEXT NOT NULL REFERENCES epics(id) ON DELETE CASCADE,
  depends_on  TEXT NOT NULL REFERENCES epics(id) ON DELETE CASCADE,
  note        TEXT,                                -- free-text reason for dependency
  PRIMARY KEY (epic_id, depends_on)
);

CREATE INDEX IF NOT EXISTS idx_deps_dependent ON deps(depends_on);

-- ── runs ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  epic_id       TEXT NOT NULL REFERENCES epics(id),
  run_dir       TEXT NOT NULL UNIQUE,              -- relative path under orch runs dir
  branch        TEXT NOT NULL,
  baseline_sha  TEXT NOT NULL,                     -- main HEAD at worktree creation
  agent         TEXT NOT NULL,                     -- 'glm' | 'kimi' | 'sonnet' | ...
  brain_picked  INTEGER NOT NULL DEFAULT 0,        -- 1 if brain-selected (vs static)
  started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ended_at      TEXT,
  final_verdict TEXT
                CHECK (final_verdict IN ('APPROVE','REQUEST_CHANGES','ESCALATE','CRASH','SALVAGED','MERGED') OR final_verdict IS NULL),
  merged_sha    TEXT,                               -- post-merge commit on main
  cost_usd      REAL DEFAULT 0,
  claude_session_id    TEXT,
  zellij_session_name  TEXT,
  last_heartbeat_at    TEXT,
  pid                  INTEGER,
  host                 TEXT,
  test_status          TEXT
    CHECK (test_status   IN ('pass','fail','skip','error') OR test_status   IS NULL),
  trace_status         TEXT
    CHECK (trace_status  IN ('pass','fail','skip','error') OR trace_status  IS NULL),
  test_trace_id        TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_epic    ON runs(epic_id);
CREATE INDEX IF NOT EXISTS idx_runs_active  ON runs(epic_id) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_runs_heartbeat
  ON runs(last_heartbeat_at) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_runs_test_status
  ON runs(test_status)  WHERE test_status  IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_runs_trace_status
  ON runs(trace_status) WHERE trace_status IS NOT NULL;

-- ── iters ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS iters (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  n             INTEGER NOT NULL,                  -- 1, 2, 3
  verdict       TEXT,
  feedback_json TEXT,                              -- full reviewer JSON {issues:[]}
  worker_log    TEXT,                              -- relative path
  cost_usd      REAL DEFAULT 0,
  exit_code     INTEGER,
  started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ended_at      TEXT,
  input_tokens          INTEGER DEFAULT 0,
  output_tokens         INTEGER DEFAULT 0,
  cache_read_tokens     INTEGER DEFAULT 0,
  cache_creation_tokens INTEGER DEFAULT 0,
  web_search_requests   INTEGER DEFAULT 0,
  web_fetch_requests    INTEGER DEFAULT 0,
  model_provider        TEXT,
  service_tier          TEXT,
  duration_seconds      INTEGER,
  debugger_verdict      TEXT,
  UNIQUE (run_id, n)
);

CREATE INDEX IF NOT EXISTS idx_iters_run ON iters(run_id);

-- ── inbox ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS inbox (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  epic_id     TEXT NOT NULL REFERENCES epics(id),
  kind        TEXT NOT NULL
              CHECK (kind IN ('escalation','stuck','scope-violation','question','human-only')),
  opened_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  resolved_at TEXT,
  resolution  TEXT,                                -- 'reset-retry' | 'override-done' | 'halt' | 'reassigned'
  body_md     TEXT NOT NULL,
  source_run_id INTEGER REFERENCES runs(id)
);

CREATE INDEX IF NOT EXISTS idx_inbox_open ON inbox(epic_id) WHERE resolved_at IS NULL;

-- ── locks ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS locks (
  name        TEXT PRIMARY KEY,                    -- 'merge', 'plane-sync', 'gauntlet'
  holder      TEXT NOT NULL,                       -- e.g. "orch-pid-81065"
  acquired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  expires_at  TEXT NOT NULL                        -- TTL for crash recovery
);

-- ── agent_profile ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_profile (
  agent                  TEXT PRIMARY KEY,
  total_runs             INTEGER NOT NULL DEFAULT 0,
  approval_rate          REAL,                     -- 0..1
  avg_iters_to_approve   REAL,
  avg_cost_per_run_usd   REAL,
  top_rejection_category TEXT,
  top_rejection_count    INTEGER,
  observed_pattern       TEXT,                     -- free-text behavioural note
  updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ── brain_decisions ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS brain_decisions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  trigger         TEXT NOT NULL
                  CHECK (trigger IN ('pick-next','reviewer-ambiguous','worker-failed','checkpoint','post-rollback','gauntlet-failed')),
  lane_filter     TEXT,
  claimable_json  TEXT NOT NULL,                   -- snapshot of claimable epics input
  decision_json   TEXT NOT NULL,                   -- {epic_id, agent, rationale, parallel_safe, override_default}
  cost_usd        REAL DEFAULT 0,
  raw_response_path TEXT                            -- relative path to raw LLM response
);

CREATE INDEX IF NOT EXISTS idx_brain_ts ON brain_decisions(ts);

-- ── subagent_runs ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS subagent_runs (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_dispatch_id       INTEGER,                -- → orch_dispatches(id) (table created in 0002)
  parent_run_id            INTEGER REFERENCES runs(id),
  parent_claude_session_id TEXT NOT NULL,
  child_claude_session_id  TEXT,
  subagent_type            TEXT,                   -- 'Explore' | 'voltagent-…'
  description              TEXT,
  prompt_excerpt           TEXT,                   -- first 240 chars
  result_excerpt           TEXT,                   -- first 480 chars
  status                   TEXT NOT NULL CHECK (status IN
    ('spawned','running','completed','failed','cancelled')),
  started_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ended_at                 TEXT,
  cwd                      TEXT,
  duration_ms              INTEGER
);

CREATE INDEX IF NOT EXISTS idx_subagent_parent_run     ON subagent_runs(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_subagent_parent_session ON subagent_runs(parent_claude_session_id);
CREATE INDEX IF NOT EXISTS idx_subagent_child_session  ON subagent_runs(child_claude_session_id);
CREATE INDEX IF NOT EXISTS idx_subagent_status         ON subagent_runs(status);

-- ── epic_agent_assignments ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS epic_agent_assignments (
  epic_id      TEXT PRIMARY KEY,                   -- may exist before epics row (scaffold)
  agent_id     TEXT NOT NULL,                      -- references config/agents/<id>.yaml
  rationale    TEXT,
  assigned_by  TEXT NOT NULL CHECK (assigned_by IN
    ('human','brain','scaffold','fallback','seed-from-yaml')),
  assigned_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_assign_agent ON epic_agent_assignments(agent_id);

CREATE TABLE IF NOT EXISTS epic_agent_assignment_history (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  epic_id       TEXT NOT NULL,
  agent_id      TEXT NOT NULL,
  rationale     TEXT,
  assigned_by   TEXT NOT NULL,
  assigned_at   TEXT NOT NULL,
  superseded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_assign_hist_epic ON epic_agent_assignment_history(epic_id);

CREATE TRIGGER IF NOT EXISTS trg_epic_agent_assignments_archive
BEFORE UPDATE ON epic_agent_assignments
FOR EACH ROW
WHEN OLD.agent_id IS NOT NEW.agent_id OR OLD.rationale IS NOT NEW.rationale
BEGIN
  INSERT INTO epic_agent_assignment_history
    (epic_id, agent_id, rationale, assigned_by, assigned_at)
  VALUES
    (OLD.epic_id, OLD.agent_id, OLD.rationale, OLD.assigned_by, OLD.assigned_at);
END;

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0001_core.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'v1');
