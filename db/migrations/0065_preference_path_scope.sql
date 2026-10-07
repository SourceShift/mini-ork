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
-- recreated below; the ``v_memory_health`` view references the table *by name*,
-- survives the drop, and rebinds to the renamed table unchanged — no view
-- recreation is needed here.

BEGIN TRANSACTION;

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

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0065_preference_path_scope.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'preference-path-scope-v1');
