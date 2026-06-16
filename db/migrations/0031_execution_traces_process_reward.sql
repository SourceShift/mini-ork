-- 0031_execution_traces_process_reward.sql
-- Track B item 3 — Process Reward Model per-node scoring (arXiv:2510.08049
-- survey + arXiv:2602.09305 PRM training). Adds a 0.0-1.0 per-node
-- reward derived heuristically from the trace's observable signals (tool
-- calls, files read/written, reviewer verdict, finish reason). The
-- promotion gate reads it to block "outcome looked fine but a mid-run
-- node was sketchy" cases.

PRAGMA foreign_keys = OFF;

ALTER TABLE execution_traces ADD COLUMN process_reward REAL DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_execution_traces_process_reward
  ON execution_traces(process_reward) WHERE process_reward IS NOT NULL;

PRAGMA foreign_keys = ON;
