-- 0069_pm_mirror.sql
-- PM layer: a local mirror of issues from mini-ork's own epics and from other
-- project-management tools (Linear, Jira, …), plus the links between them and
-- between an issue and a mini-ork run.
--
--   pm_issues  -- one row per issue, keyed by '<provider>:<key>'. An INDEX,
--                 not a source of truth: it carries what a board and a search
--                 box need, and nothing that would make a caller believe it is
--                 the whole issue (no description, no native payload).
--   pm_links   -- directed edges. `run:<run id>` is a valid uid on either end,
--                 which is how "this run is working on ENG-123" is stored
--                 without a column of its own.
--   pm_sync    -- when each provider last answered, and whether it failed.
--                 A source that is unreachable leaves the last known board on
--                 screen and notes why it may be stale.
--
-- All three are ADDITIVE. The reader is mini_ork/pm/store.py, which also runs
-- this same DDL at runtime (ensure_schema) so a state.db that never ran a
-- migration still works — the precedent set by scheduler.ensure_retry_schema.
-- tests/unit/test_pm_store.py asserts the two texts agree, so they cannot drift
-- apart quietly.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS pm_issues (
  uid             TEXT PRIMARY KEY,   -- 'local:add-retry-budget', 'linear:ENG-123'
  provider        TEXT NOT NULL,
  native_key      TEXT NOT NULL,      -- the key the provider itself uses
  title           TEXT NOT NULL,
  state           TEXT NOT NULL,      -- normalized: backlog|todo|in_progress|blocked|in_review|done|canceled
  native_state    TEXT NOT NULL,      -- the provider's own word, kept so nothing is lost
  priority        INTEGER NOT NULL DEFAULT 0,  -- HIGHER is more urgent
  native_priority INTEGER,
  url             TEXT,
  assignee        TEXT,
  labels          TEXT,               -- JSON array
  parent_uid      TEXT,
  project         TEXT,
  updated_at      TEXT,
  synced_at       TEXT NOT NULL,
  UNIQUE (provider, native_key)
);

-- Hot read: one column of the board.
CREATE INDEX IF NOT EXISTS idx_pm_issues_provider_state ON pm_issues(provider, state);

-- Hot read: "what changed lately".
CREATE INDEX IF NOT EXISTS idx_pm_issues_updated ON pm_issues(updated_at);

CREATE TABLE IF NOT EXISTS pm_links (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  from_uid   TEXT NOT NULL,
  to_uid     TEXT NOT NULL,
  kind       TEXT NOT NULL
             CHECK (kind IN ('blocks','relates','duplicates','parent','run')),
  created_at TEXT NOT NULL
);

-- The same edge twice is one edge.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pm_links_edge ON pm_links(from_uid, to_uid, kind);
CREATE INDEX IF NOT EXISTS idx_pm_links_from ON pm_links(from_uid);
CREATE INDEX IF NOT EXISTS idx_pm_links_to ON pm_links(to_uid);

CREATE TABLE IF NOT EXISTS pm_sync (
  provider  TEXT PRIMARY KEY,
  synced_at TEXT NOT NULL,
  n         INTEGER NOT NULL,
  error     TEXT
);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0069_pm_mirror.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'pm-mirror-v1');
