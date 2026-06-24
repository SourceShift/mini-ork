-- 0039_learning_column_repairs.sql
-- Repair drifted DBs where 0031/0032 are marked applied but the learning
-- columns are absent. Real-column storage is now created by db/init.sh's
-- guarded ensure_column helper (idempotent pragma_table_info check) BEFORE
-- migrations run, so these statements are guaranteed to find their backing
-- columns. The earlier sqlite_schema-text rewrite approach (which never
-- created real storage and aborted the same-session CREATE INDEX) has been
-- removed in favor of the init.sh approach.

CREATE INDEX IF NOT EXISTS idx_execution_traces_process_reward_repair
  ON execution_traces(process_reward) WHERE process_reward IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agent_perf_advantage_repair
  ON agent_performance_memory(task_class, relative_advantage DESC);
