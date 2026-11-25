-- 0094_semantic_memory_pending_cn.sql
-- Fallback must not lie: a semantic-memory write made while ContextNest is
-- unreachable is marked pending_cn=1 so `mini-ork memory sync` can replay it
-- when CN returns (docs/architecture/memory-backend.md §5).
--
--   pending_cn  INTEGER NOT NULL DEFAULT 0
--               1 = written while CN was down, awaiting replay
--               0 = nothing to replay (the normal case)
--
-- The flag is cleared ONLY by a successful `mini-ork memory sync` replay, and
-- never by a re-upsert: a mirror re-synced on every read must not erase the
-- fact that its latest content never reached CN.
--
-- ADDITIVE only, and in scope for S1 (c:1): it alters `semantic_memory` and
-- creates one index on it; `semantic_memory_uses` is untouched.
--
-- Guarded ALTER (same `.read "|sh -c …"` idiom as 0037/0041/0042/0055/0058/
-- 0093): a bare ADD COLUMN fails on reapply ("duplicate column"). The table
-- existence check keeps the pipeline safe on a DB that has not yet run 0046,
-- where ALTER would otherwise fail.

PRAGMA foreign_keys = OFF;

.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; tbl=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM sqlite_master WHERE type=\\\"table\\\" AND name=\\\"semantic_memory\\\";\"); if [ \"${tbl:-0}\" != \"0\" ]; then have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"semantic_memory\\\") WHERE name = \\\"pending_cn\\\";\"); if [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE semantic_memory ADD COLUMN pending_cn INTEGER NOT NULL DEFAULT 0;\"; fi; printf \"%s\n\" \"CREATE INDEX IF NOT EXISTS idx_semantic_memory_pending_cn ON semantic_memory(pending_cn) WHERE pending_cn = 1;\"; fi'"

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0094_semantic_memory_pending_cn.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'semantic-memory-pending-cn-v1');
