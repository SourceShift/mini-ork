-- 0055_semantic_memory_utility.sql
-- SimUtil-UCB: give semantic memory a utility signal, so retrieval stops
-- ranking on similarity alone (RetroAgent 2603.08561 §retrieval policy;
-- DeltaMem 2606.03083 failure-penalised scan).
--
-- Why this exists: a memory that reads closest to the current task is not the
-- memory most likely to help. A vague, generally-worded memory is similar to
-- everything, so it is retrieved forever and never earns its place; a narrow
-- memory that actually worked on three similar tasks keeps losing the race
-- because its wording is specific. Similarity alone cannot tell those apart —
-- only the record of what happened after the memory was used can.
--
-- ADDITIVE only. No existing column is altered, dropped, or backfilled, and
-- 0046_semantic_memory.sql is untouched (editing a shipped migration trips the
-- checksum-drift guard in mini_ork/stores/migrate.py).
--
--   uses  INTEGER NOT NULL DEFAULT 0  -- times this memory was retrieved
--   wins  INTEGER NOT NULL DEFAULT 0  -- retrievals followed by a passing run
--
-- The counters are a DERIVED INDEX, denormalised onto the row so search()
-- stays a single-table scan. semantic_memory_uses is the authoritative audit
-- trail: one row per retrieval event, resolved to win/loss when the run's
-- outcome is known. A counter that disagrees with the ledger is a bug in the
-- writer, not a fact about the world.
--
-- Attribution is the known soft spot (docs/research/rsi-techniques/11).
-- "Retrieved during a run that passed" is not proof the memory caused the pass,
-- so the utility term is Laplace-smoothed (Beta(1,1)) rather than a raw ratio,
-- and the retrieval score keeps relevance as a gate with utility+exploration
-- only reordering inside it. See mini_ork/memory/semantic.py search().

PRAGMA foreign_keys = OFF;

-- Guarded ALTER: bare ADD COLUMN fails on reapply ("duplicate column"). The
-- existence check on `uses` gates both columns — if it is present the pair was
-- added together, so the block is skipped whole (idempotent). Same
-- `.read|sh` idiom as 0037/0041/0042; db/init.sh exports MINI_ORK_DB before
-- applying, so ${MINI_ORK_DB:?} resolves to the DB being initialized.
.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"semantic_memory\\\") WHERE name = \\\"uses\\\";\"); if [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE semantic_memory ADD COLUMN uses INTEGER NOT NULL DEFAULT 0;\" \"ALTER TABLE semantic_memory ADD COLUMN wins INTEGER NOT NULL DEFAULT 0;\"; fi'"

-- The retrieval ledger. One row per (memory, run) retrieval event, written at
-- injection time by record_retrievals(); resolved to 'win'/'loss' at run end by
-- record_outcome(). 'pending' means the run never reported — the event counts
-- toward `uses` but never toward `wins`, so an unattributed retrieval can only
-- make a memory look worse, never better. Failing closed is the point.
CREATE TABLE IF NOT EXISTS semantic_memory_uses (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id    INTEGER NOT NULL,
  scope        TEXT    NOT NULL,
  run_id       TEXT    NOT NULL DEFAULT '',
  task_class   TEXT    NOT NULL DEFAULT '',
  retrieved_at REAL    NOT NULL,
  outcome      TEXT    NOT NULL DEFAULT 'pending'
               CHECK (outcome IN ('pending','win','loss'))
);

CREATE INDEX IF NOT EXISTS idx_semantic_memory_uses_run
  ON semantic_memory_uses(run_id);
CREATE INDEX IF NOT EXISTS idx_semantic_memory_uses_memory
  ON semantic_memory_uses(memory_id);
CREATE INDEX IF NOT EXISTS idx_semantic_memory_uses_pending
  ON semantic_memory_uses(run_id) WHERE outcome = 'pending';

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0055_semantic_memory_utility.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'semantic-memory-utility-v1');
