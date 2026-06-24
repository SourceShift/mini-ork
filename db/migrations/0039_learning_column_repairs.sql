-- 0039_learning_column_repairs.sql
-- Repair drifted DBs where 0031/0032 are marked applied but the learning
-- columns are absent. SQLite 3.43 has no ADD COLUMN IF NOT EXISTS, so this
-- updates sqlite_schema only when the column text is missing.

PRAGMA foreign_keys = OFF;
PRAGMA writable_schema = ON;

UPDATE sqlite_schema
   SET sql = replace(
       sql,
       'created_at            TEXT    NOT NULL DEFAULT (strftime(''%Y-%m-%dT%H:%M:%fZ'',''now''))',
       'created_at            TEXT    NOT NULL DEFAULT (strftime(''%Y-%m-%dT%H:%M:%fZ'',''now'')),
  process_reward        REAL    DEFAULT NULL'
   )
 WHERE type = 'table'
   AND name = 'execution_traces'
   AND sql NOT LIKE '%process_reward%';

UPDATE sqlite_schema
   SET sql = replace(
       sql,
       'last_updated        TEXT    NOT NULL DEFAULT (strftime(''%Y-%m-%dT%H:%M:%fZ'',''now'')),
  PRIMARY KEY (agent_version_id, task_class)',
       'last_updated        TEXT    NOT NULL DEFAULT (strftime(''%Y-%m-%dT%H:%M:%fZ'',''now'')),
  relative_advantage REAL    NOT NULL DEFAULT 0.0,
  PRIMARY KEY (agent_version_id, task_class)'
   )
 WHERE type = 'table'
   AND name = 'agent_performance_memory'
   AND sql NOT LIKE '%relative_advantage%';

PRAGMA writable_schema = RESET;

CREATE INDEX IF NOT EXISTS idx_execution_traces_process_reward_repair
  ON execution_traces(process_reward) WHERE process_reward IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agent_perf_advantage_repair
  ON agent_performance_memory(task_class, relative_advantage DESC);

PRAGMA foreign_keys = ON;
