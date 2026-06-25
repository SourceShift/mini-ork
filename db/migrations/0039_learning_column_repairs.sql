-- 0039_learning_column_repairs.sql
-- Repair drifted DBs where 0031/0032 are marked applied but the learning
-- columns are absent. Real-column storage is now created by db/init.sh's
-- guarded ensure_column helper (idempotent pragma_table_info check) BEFORE
-- migrations run, so normal init always guarantees these backing columns.
-- The repair indexes below are also self-guarded for direct sqlite3
-- application: each CREATE INDEX statement is generated only when its target
-- column already exists. The earlier sqlite_schema-text rewrite approach
-- (which never created real storage and aborted the same-session CREATE
-- INDEX) has been removed in favor of the init.sh approach.

.once /tmp/mini-ork-0039-learning-column-repairs.sql
SELECT sql
  FROM (
    SELECT CASE
      WHEN EXISTS (
        SELECT 1
          FROM pragma_table_info('execution_traces')
         WHERE name = 'process_reward'
      )
      THEN 'CREATE INDEX IF NOT EXISTS idx_execution_traces_process_reward_repair ON execution_traces(process_reward) WHERE process_reward IS NOT NULL;'
      ELSE '-- skip idx_execution_traces_process_reward_repair: execution_traces.process_reward absent'
    END AS sql
    UNION ALL
    SELECT CASE
      WHEN EXISTS (
        SELECT 1
          FROM pragma_table_info('agent_performance_memory')
         WHERE name = 'relative_advantage'
      )
      THEN 'CREATE INDEX IF NOT EXISTS idx_agent_perf_advantage_repair ON agent_performance_memory(task_class, relative_advantage DESC);'
      ELSE '-- skip idx_agent_perf_advantage_repair: agent_performance_memory.relative_advantage absent'
    END AS sql
  );
.read /tmp/mini-ork-0039-learning-column-repairs.sql
.shell rm -f /tmp/mini-ork-0039-learning-column-repairs.sql
