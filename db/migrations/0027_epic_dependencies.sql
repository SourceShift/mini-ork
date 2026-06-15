-- 0027_epic_dependencies.sql
-- E5: Cross-epic dependency graph for the autonomous multi-epic scheduler.
-- Adds (from_epic_id, to_epic_id) edges. When from_epic_id reaches status='done',
-- the resolver flips downstream epics from 'blocked' → 'not started' (ready).

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS epic_dependencies (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  from_epic_id  TEXT NOT NULL,   -- the predecessor (must reach 'done' first)
  to_epic_id    TEXT NOT NULL,   -- the dependent (starts 'blocked', unblocked on completion)
  kind          TEXT NOT NULL DEFAULT 'hard'
                CHECK (kind IN ('hard','soft','informational')),
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  resolved_at   TEXT,            -- timestamp when from_epic reached 'done'
  UNIQUE (from_epic_id, to_epic_id)
);

CREATE INDEX IF NOT EXISTS idx_epic_deps_from
  ON epic_dependencies(from_epic_id) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_epic_deps_to
  ON epic_dependencies(to_epic_id) WHERE resolved_at IS NULL;

PRAGMA foreign_keys = ON;
