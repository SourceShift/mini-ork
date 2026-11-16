-- 0093_node_attempts_move_kind.sql
-- Let a node attempt say WHAT KIND OF MOVE it was, not just whether it worked.
--
-- `node_attempts` records that a node ran and how it ended. It cannot answer
-- the question a cost review actually asks: of the attempts we paid for, how
-- many were the first try, how many redid a node that died on infrastructure,
-- how many took feedback and revised, and how many deliberately moved to a
-- different idea? Every re-attempt is currently one indistinguishable row.
--
-- move_kind:
--   NULL           — unclassified (pre-existing rows; never inferred backwards)
--   'initial'      — the node's first attempt in this run
--   'retry'        — re-ran because the previous attempt died on infrastructure
--                    (provider limit, interrupt, approval pause, retry-exhausted)
--   'revise'       — re-ran with feedback after a measured/quality failure
--   'recover'      — a resuming process re-executed interrupted work
--   'explore'      — an explicit caller declaration that this attempt tried a
--                    different idea. NEVER inferred: the ledger cannot tell a
--                    new idea from a better patch, so only the code that
--                    dispatched the attempt may claim it.
--
-- Guarded ALTER (same .read|sh idiom as 0037/0041/0042/0055/0072/0077/0078):
-- a bare ADD COLUMN fails on reapply.

PRAGMA foreign_keys = OFF;

.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; tbl=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM sqlite_master WHERE type=\\\"table\\\" AND name=\\\"node_attempts\\\";\"); if [ \"${tbl:-0}\" != \"0\" ]; then have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"node_attempts\\\") WHERE name = \\\"move_kind\\\";\"); if [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE node_attempts ADD COLUMN move_kind TEXT;\"; fi; printf \"%s\n\" \"CREATE INDEX IF NOT EXISTS idx_node_attempts_move_kind ON node_attempts(move_kind);\"; fi'"

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0093_node_attempts_move_kind.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'node-attempts-move-kind-v1');
