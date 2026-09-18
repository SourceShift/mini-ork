-- 0058_semantic_memory_attribution.sql
-- LIMBO: give each retrieval event the identity of the decision that caused it,
-- so memory spend can be attributed to a lane and a node (arXiv 2609.14138).
--
-- Why this exists: retrieval injects memories into a prompt, and the injected
-- text is prompt tokens that were never weighed against what the memories
-- bought. The ledger records that a memory was retrieved and whether the run
-- passed, but not who chose to retrieve it — so no lane can be held to the
-- retrieval cost it incurred, and the router cannot compare that cost against
-- its other terms. Recording the lane and node closes that gap. It is the
-- precondition for any budget that varies retrieval per node; without it a
-- retrieval budget would be spending against an unattributable total.
--
-- This is accounting, NOT ranking. Nothing in semantic.py's search() or
-- rank_with_prior() reads these columns; the utility signal (0055) is unchanged.
-- The retrieval count itself stays a per-call argument — this migration only
-- makes the existing spend visible.
--
-- ADDITIVE only. No existing column is altered, dropped, or backfilled;
-- 0055_semantic_memory_utility.sql and 0046_semantic_memory.sql are untouched
-- (editing a shipped migration trips the checksum-drift guard in
-- mini_ork/stores/migrate.py).
--
--   lane     TEXT NOT NULL DEFAULT ''  -- the routed lane that ran the node
--   node_id  TEXT NOT NULL DEFAULT ''  -- the node whose prompt was injected
--
-- Default '' means "not attributed" — an older writer, or a retrieval outside a
-- run, is recorded as unknown rather than guessed at. The module's own
-- bootstrap carries the same two columns (_ADDED_COLUMNS in
-- mini_ork/memory/semantic.py) so both schema paths agree.

PRAGMA foreign_keys = OFF;

-- Guarded ALTER: bare ADD COLUMN fails on reapply ("duplicate column"). The
-- existence check on `lane` gates both columns — they are added together, so if
-- one is present the block is skipped whole (idempotent). Same `.read|sh` idiom
-- as 0037/0041/0042/0055; db/init.sh exports MINI_ORK_DB before applying, so
-- ${MINI_ORK_DB:?} resolves to the DB being initialized.
.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"semantic_memory_uses\\\") WHERE name = \\\"lane\\\";\"); if [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE semantic_memory_uses ADD COLUMN lane TEXT NOT NULL DEFAULT '\"'\"''\"'\"';\" \"ALTER TABLE semantic_memory_uses ADD COLUMN node_id TEXT NOT NULL DEFAULT '\"'\"''\"'\"';\" \"CREATE INDEX IF NOT EXISTS idx_semantic_memory_uses_lane ON semantic_memory_uses(lane);\"; fi'"

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0058_semantic_memory_attribution.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'semantic-memory-attribution-v1');
