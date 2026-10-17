-- 0078_code_findings_target_repo.sql — let a harvested code finding remember
-- which repository it is about.
--
-- LF-R12. `code_findings` was one flat pile: a finding harvested from a run
-- that targeted repo A was offered to context_v2 and the "Your code" tab of
-- every later run, including runs targeting an unrelated repo B. The prompt
-- then said "Problems reviewers already found in these files. Do not repeat
-- them:" about a file the current run neither touches nor owns.
--
--   target_repo TEXT NULL  -- repo_identity(target cwd) of the run that
--                              produced the finding; NULL = ENGINE-LOCAL
--
-- NULL is the legacy shape: a row harvested before this column existed (or by
-- a run with no run_profile.json) names no repo. Readers treat it as belonging
-- to the ENGINE checkout only — it is offered when the current identity is the
-- engine's and never otherwise. NULL is never a wildcard.
--
-- ADDITIVE only. 0064 is untouched (editing a shipped migration trips the
-- checksum-drift guard in mini_ork/stores/migrate.py), and existing rows stay
-- valid as they are: every new column is NULL for them, which is exactly the
-- "no recorded repo" marker the readers already understand. The runtime
-- `ensure_schema` in mini_ork/learning/code_findings.py adds the same column,
-- so a state.db that never runs this migration still works.
--
-- The index is created unconditionally (when the table exists) because both
-- the runtime ALTER and this migration's guarded ALTER land the column first;
-- re-running the migration is a no-op thanks to IF NOT EXISTS.

PRAGMA foreign_keys = OFF;

-- Guarded ALTER: bare ADD COLUMN fails on reapply ("duplicate column"), and a
-- reapply is not hypothetical — `ensure_schema` may have added the column
-- before this migration file was ever recorded. `pragma_table_info` returns no
-- rows for a missing table, so the sqlite_master check keeps the ALTER from
-- aborting on a DB old enough to predate 0064. Same `.read|sh` idiom as
-- 0037/0041/0042/0055/0072/0077; migrate.py's _dot_env exports MINI_ORK_DB, so
-- ${MINI_ORK_DB:?} resolves to the DB being migrated.
.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; tbl=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM sqlite_master WHERE type=\\\"table\\\" AND name=\\\"code_findings\\\";\"); if [ \"${tbl:-0}\" != \"0\" ]; then have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"code_findings\\\") WHERE name = \\\"target_repo\\\";\"); if [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE code_findings ADD COLUMN target_repo TEXT;\"; fi; printf \"%s\n\" \"CREATE INDEX IF NOT EXISTS idx_code_findings_target_repo ON code_findings(target_repo);\"; fi'"

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0078_code_findings_target_repo.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'code-findings-target-repo-v1');
