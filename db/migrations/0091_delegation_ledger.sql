-- 0091_delegation_ledger.sql
-- Record WHO asked for a run, which kickoff bytes it saw, and where its row lives.
--
-- An inventory of one month of delegations matched 101 launches but only 28
-- task_runs rows. Three write-path gaps: a run launched from another project
-- writes its row into THAT project's state.db (invisible from the mini-ork
-- home), nothing records the initiator, and artifact_path/artifact_hash have
-- never been written by production code.
--
-- task_runs.initiator      TEXT  -- 'claude-session:<id>' | 'scheduler' |
--                               --   'recover:<run_id>' | 'rsi-cycle:<id>' | 'manual[:<uid>]'
-- task_runs.kickoff_copy   TEXT  -- run-dir-relative filename of the copied kickoff
-- task_runs.kickoff_sha256 TEXT  -- sha256 of the kickoff bytes at dispatch
-- task_runs.home_db        TEXT  -- ABSOLUTE path of the state.db holding this row
--
-- Guarded ALTER (same .read|sh idiom as 0037/0041/0042/0055/0072/0077/0078/0090):
-- a bare ADD COLUMN fails on reapply, and every migration in this directory
-- reapplies. The sqlite_master probe also makes this a no-op on a DB that never
-- had task_runs — no half-state.
--
-- One pragma_table_info probe per column (the .read line below carries all four
-- on one line because a dot-command is line-based):
--   pragma_table_info("task_runs") WHERE name = "initiator"
--   pragma_table_info("task_runs") WHERE name = "kickoff_copy"
--   pragma_table_info("task_runs") WHERE name = "kickoff_sha256"
--   pragma_table_info("task_runs") WHERE name = "home_db"

PRAGMA foreign_keys = OFF;

.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; tbl=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM sqlite_master WHERE type=\\\"table\\\" AND name=\\\"task_runs\\\";\"); if [ \"${tbl:-0}\" != \"0\" ]; then h=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"task_runs\\\") WHERE name = \\\"initiator\\\";\"); if [ \"${h:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE task_runs ADD COLUMN initiator TEXT;\"; fi; h=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"task_runs\\\") WHERE name = \\\"kickoff_copy\\\";\"); if [ \"${h:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE task_runs ADD COLUMN kickoff_copy TEXT;\"; fi; h=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"task_runs\\\") WHERE name = \\\"kickoff_sha256\\\";\"); if [ \"${h:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE task_runs ADD COLUMN kickoff_sha256 TEXT;\"; fi; h=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"task_runs\\\") WHERE name = \\\"home_db\\\";\"); if [ \"${h:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE task_runs ADD COLUMN home_db TEXT;\"; fi; printf \"%s\n\" \"CREATE INDEX IF NOT EXISTS idx_task_runs_initiator ON task_runs(initiator);\"; fi'"

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0091_delegation_ledger.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'delegation-ledger-v1');
