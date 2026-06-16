-- 0034_topology_role_evolution.sql
-- Meta-orchestrator (mini-ork-of-mini-orks) Phase 1.
-- Two tables for topology and role co-evolution per the design grounded
-- in Shepherd (arXiv:2605.10913), TacoMAS (2605.09539), Mass (2502.02533),
-- TCP-MCP (2605.27850) and EvoChamber (2605.11136).

PRAGMA foreign_keys = OFF;

-- Per-(topology_id, task_class) win-rate, analogous to prompt_win_rates from
-- migration 0030 but at the workflow-graph level. A "topology" is a named
-- workflow.yaml graph (its yaml_hash from workflow_memory).
CREATE TABLE IF NOT EXISTS topology_win_rates (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  topology_id      TEXT    NOT NULL,   -- yaml_hash from workflow_memory
  workflow_name    TEXT    NOT NULL,   -- e.g. 'framework-edit'
  task_class       TEXT    NOT NULL,
  wins             INTEGER NOT NULL DEFAULT 0,
  losses           INTEGER NOT NULL DEFAULT 0,
  ties             INTEGER NOT NULL DEFAULT 0,
  win_rate         REAL    NOT NULL DEFAULT 0.0,
  sample_size      INTEGER NOT NULL DEFAULT 0,
  avg_cost_usd     REAL    NOT NULL DEFAULT 0.0,
  avg_duration_ms  REAL    NOT NULL DEFAULT 0.0,
  last_updated     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (topology_id, task_class)
);
CREATE INDEX IF NOT EXISTS idx_topology_wr_class
  ON topology_win_rates(task_class, win_rate DESC);
CREATE INDEX IF NOT EXISTS idx_topology_wr_name
  ON topology_win_rates(workflow_name, task_class, win_rate DESC);

-- Audit trail for role-evolution proposals. Each row records a proposed
-- change to a recipe's role assignment (split, merge, rename, retire)
-- with its evidence and outcome. Idempotence on (proposed_at, target_recipe,
-- proposal_kind) is enforced by the application, not the DB.
CREATE TABLE IF NOT EXISTS role_evolver_log (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  proposed_at      INTEGER NOT NULL,
  target_recipe    TEXT    NOT NULL,
  target_node_id   TEXT,                -- specific node within the recipe
  proposal_kind    TEXT    NOT NULL
                   CHECK (proposal_kind IN ('split','merge','rename','retire','add')),
  rationale        TEXT    NOT NULL,    -- short prose
  evidence_json    TEXT    NOT NULL DEFAULT '{}',  -- {bug_ids:[], agent_perf_rows:[], gradient_ids:[]}
  proposed_change  TEXT    NOT NULL,    -- YAML/text diff describing the change
  status           TEXT    NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','accepted','rejected','superseded','applied')),
  applied_at       INTEGER,
  benchmark_delta  REAL                 -- post-application win-rate delta if measured
);
CREATE INDEX IF NOT EXISTS idx_role_evol_recipe ON role_evolver_log(target_recipe);
CREATE INDEX IF NOT EXISTS idx_role_evol_status ON role_evolver_log(status);

-- Conductor decisions log: every (epic, chosen_topology, chosen_lane,
-- predicted_advantage) the conductor commits. Used for post-hoc validation
-- (TacoMAS-style co-evolution feedback).
CREATE TABLE IF NOT EXISTS conductor_decisions (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  decided_at         INTEGER NOT NULL,
  epic_id            TEXT    NOT NULL,
  task_class         TEXT,
  chosen_topology    TEXT,
  chosen_recipe      TEXT,
  chosen_lane_hints  TEXT,                -- JSON map {node_type: lane_id}
  predicted_score    REAL    NOT NULL DEFAULT 0.0,
  budget_pct_used    REAL    NOT NULL DEFAULT 0.0, -- 24h spend / cap at decision time
  rationale          TEXT,                -- compact explanation (SQL-source citations)
  outcome            TEXT,                -- 'success'/'failure'/'pending'
  realized_score     REAL                 -- post-run win/loss → score
);
CREATE INDEX IF NOT EXISTS idx_conductor_epic ON conductor_decisions(epic_id);
CREATE INDEX IF NOT EXISTS idx_conductor_decided_at ON conductor_decisions(decided_at);

PRAGMA foreign_keys = ON;
