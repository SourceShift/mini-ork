-- 0065_preference_path_scope.sql — extend ``user_preference_memory`` with a
-- 'path' scope, so a preference can be attached to a file glob and reach ONLY
-- the runs whose kickoff touches a matching path (plan 2026-10-07, step E2;
-- kickoff kickoffs/auto/eng-path-rules.md).
--
-- Number: this branch's db/migrations/ tops out at 0063; 0064 is reserved for
-- the sibling E1 worktree. Discovery is ``sorted(Path(...).glob("*.sql"))`` with
-- a "pending set is a suffix" invariant (mini_ork/stores/migrate.py:429,155),
-- so the 0064 gap is harmless — 0065 still applies after every present file.
--
-- SQLite cannot alter a CHECK constraint in place, so the table is rebuilt:
-- create-new → copy rows → drop-old → rename. The whole rebuild is wrapped in
-- an explicit transaction (the runner honours a migration that manages its own
-- BEGIN — migrate.py:387-390).
--
-- DROP TABLE takes the table's three indexes with it (verified), so they are
-- recreated below. It ALSO leaves ``v_memory_health`` dangling: the live view
-- selects ``COUNT(*)`` from ``user_preference_memory`` by name. On SQLite
-- >= 3.45 (``legacy_alter_table`` off — the default under the Python 3.13
-- runtime) ``ALTER TABLE … RENAME`` re-parses every view in the schema and
-- aborts with ``error in view v_memory_health: no such table:
-- main.user_preference_memory`` (reproduced on a backup of the live state.db).
-- SQL cannot introspect, so the one known dependent view is dropped first and
-- recreated byte-for-byte — the exact ``SELECT sql FROM sqlite_master WHERE
-- name='v_memory_health'`` captured from the live DB — after the rename.
-- ``mini_ork.memory.preferences.ensure_schema`` mirrors this generically,
-- introspecting ``sqlite_master`` for every dependent view and trigger.

BEGIN TRANSACTION;

-- 1. Drop the dependent view before the rebuild. Leaving it in place makes the
--    ALTER TABLE RENAME below fail on SQLite >= 3.45 (see header).
DROP VIEW IF EXISTS v_memory_health;

-- 2. Rebuild the table with 'path' added to the CHECK.
CREATE TABLE user_preference_memory_new (
  user_id             TEXT    NOT NULL,
  preference_key      TEXT    NOT NULL,
  preference_value    TEXT    NOT NULL DEFAULT '{}', -- JSON scalar or object
  scope               TEXT    NOT NULL DEFAULT 'global'
                      CHECK (scope IN ('global','task_class','workflow','path')),
  scope_target        TEXT    NOT NULL DEFAULT '',   -- task_class / workflow / glob
  set_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (user_id, preference_key, scope, scope_target)
);

INSERT INTO user_preference_memory_new
  (user_id, preference_key, preference_value, scope, scope_target, set_at)
SELECT user_id, preference_key, preference_value, scope, scope_target, set_at
FROM user_preference_memory;

DROP TABLE user_preference_memory;

ALTER TABLE user_preference_memory_new RENAME TO user_preference_memory;

CREATE INDEX IF NOT EXISTS idx_user_pref_user_id ON user_preference_memory(user_id);
CREATE INDEX IF NOT EXISTS idx_user_pref_key     ON user_preference_memory(preference_key);
CREATE INDEX IF NOT EXISTS idx_user_pref_scope   ON user_preference_memory(scope);

-- 3. Recreate the dependent view from its exact live definition (the table now
--    exists again, so the view rebinds to the rebuilt table).
CREATE VIEW v_memory_health AS
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

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0065_preference_path_scope.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'preference-path-scope-v1');
