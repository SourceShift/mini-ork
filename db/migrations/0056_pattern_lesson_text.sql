-- 0056_pattern_lesson_text.sql
-- Give emergent patterns an authored lesson, so the block injected into planner
-- and worker prompts stops being a cluster statistic (Trace2Skill 2603.25158
-- Stage 2/3: analyst sub-agents propose patches, a merge operator consolidates).
--
-- Why this exists: `pattern_store.mine_from_traces` is a GROUP BY over
-- (task_class, status). It writes `description` as the cluster key rendered as
-- prose — "cluster: task_class=recipe_authoring status=success (freq=46 in
-- window)" — and `pattern_id` is sha256 of that same key, so the label carries
-- no information the id did not already carry. Every approved pattern is then
-- injected into the prompt as though it were guidance. It is a frequency count.
--
-- ADDITIVE only. `description` and `cluster_label` keep their current meaning
-- (the cluster key), the deterministic miner is untouched, and a database with
-- no `lesson_text` behaves exactly as it does today — the read-back falls back
-- to the label. Nothing is backfilled, because there is nothing to backfill
-- from: no model has ever read these traces. Backfilling a label into a lesson
-- column would launder a statistic into guidance, which is the whole defect.
--
--   lesson_text TEXT NULL  -- model-authored guidance for this cluster, or NULL
--
-- NULL is the honest default and the common case on any database that predates
-- this column. `mini_ork/learning/pattern_induction.py` is the only writer.

PRAGMA foreign_keys = OFF;

-- Guarded ALTER: bare ADD COLUMN fails on reapply ("duplicate column"), and
-- `pragma_table_info` returns no rows for a table that does not exist — so
-- without the sqlite_master check the ALTER would be emitted against a missing
-- table and abort the migration. Both conditions are required. Same `.read|sh`
-- idiom as 0037/0041/0042/0055; db/init.sh exports MINI_ORK_DB before applying,
-- so ${MINI_ORK_DB:?} resolves to the DB being initialized.
.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; tbl=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM sqlite_master WHERE name=\\\"pattern_records\\\";\"); have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"pattern_records\\\") WHERE name = \\\"lesson_text\\\";\"); if [ \"${tbl:-0}\" != \"0\" ] && [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE pattern_records ADD COLUMN lesson_text TEXT;\"; fi'"

.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; tbl=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM sqlite_master WHERE name=\\\"emergent_patterns\\\";\"); have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"emergent_patterns\\\") WHERE name = \\\"lesson_text\\\";\"); if [ \"${tbl:-0}\" != \"0\" ] && [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE emergent_patterns ADD COLUMN lesson_text TEXT;\"; fi'"

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0056_pattern_lesson_text.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'pattern-lesson-text-v1');
