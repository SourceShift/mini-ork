-- v_memory_health: per-namespace row counts and last-write timestamp.
-- Used by `mini-ork doctor` to give a quick health readout of all 8 memory
-- namespaces without needing to query each table individually.
--
-- Each row: namespace | row_count | last_write (ISO timestamp or null if empty)
CREATE VIEW IF NOT EXISTS v_memory_health AS
SELECT
  'task_memory'            AS namespace,
  COUNT(*)                 AS row_count,
  MAX(created_at)          AS last_write
FROM task_memory

UNION ALL

SELECT
  'workflow_memory',
  COUNT(*),
  MAX(created_at)
FROM workflow_memory

UNION ALL

SELECT
  'agent_performance_memory',
  COUNT(*),
  MAX(last_updated)
FROM agent_performance_memory

UNION ALL

SELECT
  'failure_memory',
  COUNT(*),
  MAX(occurred_at)
FROM failure_memory

UNION ALL

SELECT
  'recovery_memory',
  COUNT(*),
  MAX(recovered_at)
FROM recovery_memory

UNION ALL

SELECT
  'user_preference_memory',
  COUNT(*),
  MAX(set_at)
FROM user_preference_memory

UNION ALL

SELECT
  'artifact_memory',
  COUNT(*),
  MAX(produced_at)
FROM artifact_memory

UNION ALL

SELECT
  'benchmark_memory',
  COUNT(*),
  MAX(ran_at)
FROM benchmark_memory;
