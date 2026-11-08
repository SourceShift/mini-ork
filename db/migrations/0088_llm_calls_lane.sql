-- 0088_llm_calls_lane.sql — let a cost row name the lane that served it.
--
-- `llm_calls` recorded provider, model_id, feature_name and actor, but no
-- lane, so per-route cost could not be attributed: the resolved lane was in
-- hand at every write site and stored only under `model_id`, never under a
-- field a page could GROUP BY.
--
--   lane  TEXT   the lane (or, for the CLI/diagnoser writers, the model id)
--                that produced the row; NULL for every row written before
--                this column existed
--
-- NULLABLE with NO DEFAULT, and that is the point: NULL means "written
-- before this column existed", which a page must be able to tell apart from
-- a lane named '' — a DEFAULT would collapse the two, and a backfill would
-- invent attribution the old rows never carried. The 16,429 existing rows
-- stay NULL.
--
-- Vocabulary note for later readers: the llm_dispatch writers store a
-- routing lane (glm, sonnet, …), while persist_call stores a concrete model
-- id (glm-4.6). Two zero-cost system rows (guard refusal and dispatcher
-- crash) record the requested *chain* (e.g. "glm,sonnet") because that is
-- the `model_id` they already pass; they never carry cost, so a GROUP BY
-- shows a phantom zero-cost group but no attribution error. Do not repoint
-- check_lane_fuse at this column: it has no PRAGMA gate, so an un-migrated
-- ledger would raise "no such column: lane"; `model_id` stays its key.
--
-- ADDITIVE only. No existing column is altered, dropped or backfilled.

PRAGMA foreign_keys = OFF;

-- Guarded ALTER: bare ADD COLUMN fails on reapply ("duplicate column"), and a
-- reapply is not hypothetical — a hand-applied ALTER can land before this file
-- is ever recorded. Same `.read|sh` idiom as 0072/0074/0075; migrate.py's
-- _dot_env exports MINI_ORK_DB.
.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"llm_calls\\\") WHERE name = \\\"lane\\\";\"); if [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE llm_calls ADD COLUMN lane TEXT;\"; fi'"

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0088_llm_calls_lane.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'llm-calls-lane-v1');
