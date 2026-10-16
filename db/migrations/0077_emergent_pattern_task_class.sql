-- 0077_emergent_pattern_task_class.sql — let an emergent pattern say which
-- task class it is about, so selection can scope on it.
--
-- LF-R2. `_approved_emergent_rows` handed every approved pattern to every run,
-- so 72% of injections (1928 of 2670) were cross-class: the code_fix reviewer of
-- a live run read patterns about `__classify__`, `__reflect__`, goal_loop and
-- self_migrate, plus exactly one code_fix pattern. The class was always there —
-- every cluster label carries it as prose ("cluster: task_class=code_fix
-- status=failure (freq=11 in window)") — but only as a token inside a string
-- nothing parsed.
--
--   task_class TEXT NULL  -- the class the cluster was mined under, or NULL
--
-- ADDITIVE only, and the column is a convenience rather than a dependency:
-- `context_assembler.pattern_task_class` applies the same parse in Python and
-- the read-back falls back to it whenever the column is absent (a database that
-- never runs this migration) or blank (a row an older pipeline wrote into a
-- migrated one). Selection is therefore correct on both shapes, which is what
-- lets this ship to the live DB on its own schedule.
--
-- The backfill parses `cluster_label` with the same rule as the Python parse:
-- the token after `task_class=`, up to the next space or the end of the label;
-- a label with no such token keeps NULL — an unknown class is not a class, and
-- NULL never matches a run's own class. `NULLIF(substr(…), '')` covers the
-- degenerate `task_class=` with an empty token, which the Python side also
-- reports as no class rather than as ''.

PRAGMA foreign_keys = OFF;

-- Guarded ALTER: bare ADD COLUMN fails on reapply ("duplicate column"), and
-- `pragma_table_info` returns no rows for a table that does not exist — so
-- without the sqlite_master check the ALTER would be emitted against a missing
-- table and abort the migration. Both conditions are required. Same `.read|sh`
-- idiom as 0037/0041/0042/0055/0056; migrate.py's _dot_env exports MINI_ORK_DB,
-- so ${MINI_ORK_DB:?} resolves to the DB being migrated.
.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; tbl=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM sqlite_master WHERE name=\\\"emergent_patterns\\\";\"); have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"emergent_patterns\\\") WHERE name = \\\"task_class\\\";\"); if [ \"${tbl:-0}\" != \"0\" ] && [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE emergent_patterns ADD COLUMN task_class TEXT;\"; fi'"

-- Backfill, emitted whenever the table exists. Idempotent by construction: the
-- parse is a pure function of `cluster_label`, so re-running it over an
-- already-backfilled table reproduces the same values. `instr(rest || ' ', ' ')`
-- gives every label a delimiter, so a class at the end of the label is parsed
-- by the same expression as one in the middle.
.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; tbl=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM sqlite_master WHERE name=\\\"emergent_patterns\\\";\"); if [ \"${tbl:-0}\" != \"0\" ]; then printf \"%s\n\" \"UPDATE emergent_patterns SET task_class = NULLIF(substr(substr(cluster_label, instr(cluster_label, '\\''task_class='\\'') + 11), 1, instr(substr(cluster_label, instr(cluster_label, '\\''task_class='\\'') + 11) || '\\'' '\\'', '\\'' '\\'') - 1), '\\'''\\'') WHERE instr(cluster_label, '\\''task_class='\\'') > 0;\"; fi'"

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0077_emergent_pattern_task_class.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'emergent-pattern-task-class-v1');
