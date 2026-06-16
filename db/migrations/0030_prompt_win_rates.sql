-- 0030_prompt_win_rates.sql
-- Track A item 2 — RHO (Retrospective Harness Optimization, arXiv:2606.05922)
-- For every (prompt_version_hash, task_class) pair, aggregate downstream
-- verifier outcomes into a win/loss/win_rate record. The promotion gate +
-- the apo-prompt-tune recipe both read this to identify which prompt
-- variants outperform their peers.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS prompt_win_rates (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  prompt_version_hash TEXT NOT NULL,
  task_class          TEXT NOT NULL,
  node_type           TEXT,               -- planner/implementer/reviewer/...
  agent_role          TEXT,               -- matches execution_traces.agent_version_id when present
  wins                INTEGER NOT NULL DEFAULT 0,
  losses              INTEGER NOT NULL DEFAULT 0,
  ties                INTEGER NOT NULL DEFAULT 0,   -- vacuous / unknown / running
  win_rate            REAL    NOT NULL DEFAULT 0.0, -- wins / (wins + losses)
  sample_size         INTEGER NOT NULL DEFAULT 0,   -- wins + losses + ties
  last_updated        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (prompt_version_hash, task_class)
);

CREATE INDEX IF NOT EXISTS idx_prompt_win_rates_class
  ON prompt_win_rates(task_class, win_rate DESC);
CREATE INDEX IF NOT EXISTS idx_prompt_win_rates_node
  ON prompt_win_rates(node_type, task_class, win_rate DESC) WHERE node_type IS NOT NULL;

PRAGMA foreign_keys = ON;
