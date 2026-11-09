-- 0090_apply_attempts_reason_class.sql
-- Let an apply attempt say whether it failed on EVIDENCE or on INFRASTRUCTURE.
--
-- `_previously_failed` treats every quarantined/rejected apply_attempts row as
-- "this directive already failed its gate, never re-propose it". That is right
-- for a measured failure and wrong for an infrastructure one: a busy database
-- or an unreadable certificate quarantines the same row, so one bad second
-- permanently blacklists a directive that in fact measured an improvement.
--
-- reason_class:
--   NULL / ''  — unclassified (pre-existing rows; still counts as evidence,
--                the conservative default)
--   'evidence' — the gate measured and refused
--   'infra'    — the run could not measure; never blocks a re-proposal
--
-- Guarded ALTER (same .read|sh idiom as 0037/0041/0042/0055/0072/0077/0078):
-- a bare ADD COLUMN fails on reapply.

PRAGMA foreign_keys = OFF;

.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; tbl=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM sqlite_master WHERE type=\\\"table\\\" AND name=\\\"apply_attempts\\\";\"); if [ \"${tbl:-0}\" != \"0\" ]; then have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"apply_attempts\\\") WHERE name = \\\"reason_class\\\";\"); if [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE apply_attempts ADD COLUMN reason_class TEXT;\"; fi; printf \"%s\n\" \"CREATE INDEX IF NOT EXISTS idx_apply_attempts_reason_class ON apply_attempts(reason_class);\"; fi'"

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0090_apply_attempts_reason_class.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'apply-attempts-reason-class-v1');
