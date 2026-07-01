-- 0046_semantic_memory.sql
-- mem-a (Track mem-a): storage for the standalone, opt-in semantic long-term
-- memory module (mini_ork.memory). Additive only — no backfill, no ALTER on
-- existing tables. The Python module (mini_ork/memory/semantic.py) bootstraps
-- this table against a per-test tmp DB via `_connect`, so the runtime path
-- does not depend on `mini-ork init` having run.
--
-- Columns match the kickoff contract verbatim:
--   id, scope, text, embedding (BLOB of unit-normalized float32), created_at, meta
--
-- Idempotency: brand-new table, so CREATE TABLE IF NOT EXISTS +
-- CREATE INDEX IF NOT EXISTS is sufficient (mirrors 0043_lane_domain_advantage
-- and 0045_defect_attributions). The INSERT OR IGNORE INTO schema_migrations
-- at the bottom keeps `mini-ork init` re-runs stable.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS semantic_memory (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  scope      TEXT    NOT NULL,
  text       TEXT    NOT NULL,
  embedding  BLOB    NOT NULL,                  -- packed unit-normalized float32 (little-endian)
  created_at REAL    NOT NULL,                  -- unix epoch seconds
  meta       TEXT                                -- free-form JSON, may be NULL
);

-- Primary access pattern: ranked cosine search within a scope.
-- mem-a's `search()` always filters by scope before ORDER BY score.
CREATE INDEX IF NOT EXISTS idx_semantic_memory_scope
  ON semantic_memory(scope);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0046_semantic_memory.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'semantic-memory-v1');
