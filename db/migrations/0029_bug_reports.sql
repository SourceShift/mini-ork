-- 0029_bug_reports.sql
-- Per-agent observed-but-deferred bug reporting channel. Each agent emits to
-- ${MINI_ORK_RUN_DIR}/noticed_bugs.jsonl during its dispatch. Reflect sweeps
-- and dedupes into this table. The scheduler can promote top-N into new
-- epics (queued status = 'queued_as_epic' once an epic is created).

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS bug_reports (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  -- Identity / provenance
  fingerprint          TEXT NOT NULL UNIQUE,          -- sha256(normalized title) for dedupe
  run_id               TEXT,                          -- first observing run
  agent_role           TEXT NOT NULL,                 -- planner|implementer|reviewer|researcher|verifier|...
  task_class           TEXT,
  observed_in          TEXT,                          -- file path / module / "general"

  -- Content
  title                TEXT NOT NULL,                 -- one-line bug description
  description          TEXT NOT NULL DEFAULT '',      -- full prose
  suggested_fix        TEXT,                          -- agent's proposed remediation

  -- Triage
  severity             TEXT NOT NULL DEFAULT 'medium'
                         CHECK (severity IN ('low','medium','high','critical')),
  confidence           REAL NOT NULL DEFAULT 0.5,     -- agent's confidence 0.0-1.0
  frequency            INTEGER NOT NULL DEFAULT 1,    -- dedup increments this

  -- Lifecycle
  status               TEXT NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open','queued_as_epic','wontfix','dupe','resolved')),
  promoted_to_epic_id  TEXT,                          -- FK to epics(id) when scheduler creates an epic

  -- Timestamps
  first_seen_at        INTEGER NOT NULL,              -- epoch seconds
  last_seen_at         INTEGER NOT NULL,
  updated_at           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bug_reports_status
  ON bug_reports(status) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_bug_reports_severity
  ON bug_reports(severity, frequency DESC) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_bug_reports_task_class
  ON bug_reports(task_class) WHERE task_class IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bug_reports_promoted
  ON bug_reports(promoted_to_epic_id) WHERE promoted_to_epic_id IS NOT NULL;

PRAGMA foreign_keys = ON;
