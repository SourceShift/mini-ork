-- 0032_lane_relative_advantage.sql
-- Track B item 4 — GRPO-style lane routing with relative advantage.
-- arXiv:2601.22607 (verifiable-reward GRPO) + 2603.02701 (Graph-GRPO,
-- group-relative comparison for multi-agent topologies).
--
-- Each lens panel dispatch produces N comparable trajectories (codex_lens,
-- kimi_lens, glm_lens, minimax_lens). We compute per-lens
-- relative_advantage = score(lens) - mean(scores in group). Positive
-- advantage means "this lens outperformed its peers"; negative means
-- "underperformed". Over many runs, lanes with positive advantage in a
-- given (task_class, node_type) get higher routing priority.
--
-- agent_performance_memory already exists (migration 0009). This migration
-- only adds the relative_advantage column.

PRAGMA foreign_keys = OFF;

ALTER TABLE agent_performance_memory
  ADD COLUMN relative_advantage REAL NOT NULL DEFAULT 0.0;

CREATE INDEX IF NOT EXISTS idx_agent_perf_advantage
  ON agent_performance_memory(task_class, relative_advantage DESC);

PRAGMA foreign_keys = ON;
