-- mini-ork migration 0003 — tickets, gauntlet, detective classifications
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0003_tickets_gauntlet.sql
BEGIN;

-- ── tickets ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tickets (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id       TEXT UNIQUE NOT NULL,
  parent_epic_id  TEXT NOT NULL REFERENCES epics(id),
  category        TEXT NOT NULL,                   -- open text; project-defined (e.g. 'route_404','auth_wire','contract_drift','crash','ux_regression','other')
  severity        TEXT NOT NULL CHECK (severity IN ('blocker','major','minor')),
  source          TEXT NOT NULL,
  page_route      TEXT,
  file_hint       TEXT,
  evidence        TEXT NOT NULL,
  fix_brief       TEXT,
  status          TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open','claimed','in_progress','done','wontfix','duplicate','dup-pending')),
  claimed_by      TEXT,
  detected_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  fixed_at        TEXT,
  fixed_in_commit TEXT,
  fingerprint     TEXT,
  expected_feature_id INTEGER,                     -- → expected_features(id) (table in 0004)
  refined_count   INTEGER NOT NULL DEFAULT 0,
  evidence_history TEXT NOT NULL DEFAULT '[]',
  validation_status TEXT,
  validation_evidence TEXT,
  bridge_plan     TEXT,
  bridge_status   TEXT,
  superseded_by   TEXT REFERENCES tickets(ticket_id),
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TEXT,
  journey_id      TEXT                             -- → journeys(id) (table in 0005)
);

CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_parent ON tickets(parent_epic_id);
CREATE INDEX IF NOT EXISTS idx_tickets_category ON tickets(category);
CREATE INDEX IF NOT EXISTS idx_tickets_fingerprint ON tickets(fingerprint) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_tickets_superseded_by ON tickets(superseded_by) WHERE status='dup-pending';
CREATE INDEX IF NOT EXISTS idx_tickets_journey ON tickets(journey_id);

CREATE TRIGGER IF NOT EXISTS trg_tickets_fingerprint_immutable
BEFORE UPDATE OF fingerprint ON tickets
FOR EACH ROW
WHEN OLD.fingerprint IS NOT NULL
  AND NEW.fingerprint IS NOT NULL
  AND NEW.fingerprint != OLD.fingerprint
BEGIN
  SELECT RAISE(ABORT, 'tickets.fingerprint is immutable post-insert; use superseded_by or status=duplicate for dedup.');
END;

CREATE TRIGGER IF NOT EXISTS trg_promote_dup_pending
AFTER UPDATE OF status ON tickets
WHEN NEW.status = 'done' AND OLD.status != 'done'
BEGIN
  UPDATE tickets SET status = 'duplicate'
   WHERE superseded_by = NEW.ticket_id AND status = 'dup-pending';
END;

CREATE TRIGGER IF NOT EXISTS trg_revert_dup_pending
AFTER UPDATE OF status ON tickets
WHEN NEW.status = 'open' AND OLD.status = 'in_progress'
BEGIN
  UPDATE tickets SET status = 'open'
   WHERE superseded_by = NEW.ticket_id AND status = 'dup-pending';
END;

CREATE TRIGGER IF NOT EXISTS trg_cascade_canonical_wontfix
AFTER UPDATE OF status ON tickets
WHEN NEW.status = 'wontfix' AND OLD.status != 'wontfix'
BEGIN
  -- Promote the highest-evidence duplicate to fresh canonical
  UPDATE tickets
     SET status='open',
         attempts=0,
         superseded_by=NULL,
         claimed_by=NULL,
         last_attempt_at=NULL,
         evidence = COALESCE(evidence,'') || char(10) || char(10) ||
           '[trg_cascade_canonical_wontfix ' ||
           strftime('%Y-%m-%dT%H:%M:%fZ','now') ||
           '] promoted from duplicate after canonical ' || NEW.ticket_id ||
           ' was wontfixed.'
   WHERE ticket_id = (
     SELECT ticket_id FROM tickets
      WHERE superseded_by = NEW.ticket_id
        AND status IN ('duplicate','dup-pending')
      ORDER BY length(COALESCE(evidence,'')) DESC,
               CASE severity WHEN 'blocker' THEN 0 WHEN 'major' THEN 1 ELSE 2 END,
               ticket_id ASC
      LIMIT 1
   );

  -- Cascade-wontfix the remaining duplicates
  UPDATE tickets
     SET status='wontfix',
         claimed_by=NULL,
         evidence = COALESCE(evidence,'') || char(10) ||
           '[trg_cascade_canonical_wontfix] cascade-wontfix; canonical ' ||
           NEW.ticket_id || ' hit terminal wontfix.'
   WHERE superseded_by = NEW.ticket_id
     AND status IN ('duplicate','dup-pending');
END;

-- ── ticket_attempts ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ticket_attempts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id       TEXT    NOT NULL,
  attempt_number  INTEGER NOT NULL,                -- 1-based round counter
  agent           TEXT    NOT NULL,
  started_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ended_at        TEXT,
  outcome         TEXT,
  outcome_detail  TEXT,
  branch          TEXT,
  run_dir         TEXT,
  fixed_in_commit TEXT,
  worker_exit     INTEGER,
  category        TEXT,                            -- echo of tickets.category at dispatch time
  severity        TEXT,
  CHECK (outcome IS NULL OR outcome IN (
    'merged','gauntlet_failed','no_commits','timeout','api_error','superseded',
    'cancelled','validator_failed','unknown'
  ))
);

CREATE INDEX IF NOT EXISTS idx_ta_ticket   ON ticket_attempts(ticket_id);
CREATE INDEX IF NOT EXISTS idx_ta_agent    ON ticket_attempts(agent);
CREATE INDEX IF NOT EXISTS idx_ta_started  ON ticket_attempts(started_at);
CREATE INDEX IF NOT EXISTS idx_ta_outcome  ON ticket_attempts(outcome);

-- ── detective_classifications ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS detective_classifications (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  epic_id         TEXT NOT NULL,
  classification  TEXT NOT NULL
                  CHECK (classification IN ('baseline_rot','dual_types_trap','scope_violation','real_bug','infra_failure','unknown')),
  confidence      REAL DEFAULT 0,
  rationale       TEXT,
  detected_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  source_run_id   INTEGER REFERENCES runs(id),
  promoted_ticket TEXT REFERENCES tickets(ticket_id)
);

CREATE INDEX IF NOT EXISTS idx_dc_epic           ON detective_classifications(epic_id);
CREATE INDEX IF NOT EXISTS idx_dc_classification ON detective_classifications(classification);
CREATE INDEX IF NOT EXISTS idx_dc_detected       ON detective_classifications(detected_at);

CREATE TABLE IF NOT EXISTS detective_blocker_files (
  classification_id INTEGER NOT NULL REFERENCES detective_classifications(id) ON DELETE CASCADE,
  file_path         TEXT NOT NULL,
  PRIMARY KEY (classification_id, file_path)
);

CREATE INDEX IF NOT EXISTS idx_dbf_path ON detective_blocker_files(file_path);

-- ── missed_by_gauntlet ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS missed_by_gauntlet (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  detected_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  reporter     TEXT NOT NULL,                      -- 'human' | 'auto-probe' | 'user-bug-report'
  epic_id      TEXT,
  category     TEXT NOT NULL,                      -- open text; e.g. 'route_404','auth_wire','contract_drift','crash','ux_regression','other'
  description  TEXT NOT NULL,
  evidence     TEXT,
  fixed_in     TEXT,                               -- commit sha or 'pending'
  promoted_to_gauntlet BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_missed_by_category ON missed_by_gauntlet(category);
CREATE INDEX IF NOT EXISTS idx_missed_by_epic     ON missed_by_gauntlet(epic_id);

-- ── gauntlet_failures ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gauntlet_failures (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id   TEXT NOT NULL,
  attempt_no  INTEGER,
  fingerprint TEXT NOT NULL,
  stage       TEXT,
  failed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  log_path    TEXT
);

CREATE INDEX IF NOT EXISTS idx_gf_fp        ON gauntlet_failures(fingerprint);
CREATE INDEX IF NOT EXISTS idx_gf_ticket    ON gauntlet_failures(ticket_id);
CREATE INDEX IF NOT EXISTS idx_gf_failed_at ON gauntlet_failures(failed_at);

-- ── lessons_bank ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lessons_bank (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  failure_class        TEXT NOT NULL CHECK (failure_class IN (
    'baseline_rot','stale_branch','missing_kickoff','scope_violation',
    'typecheck_regression','lint_regression','merge_conflict','rate_limit',
    'auth_error','context_overflow','subagent_unsupported','spec_failure',
    'gauntlet_failure','unknown'
  )),
  kickoff_pattern      TEXT,                       -- regex/glob for kickoff title or scope
  error_pattern        TEXT,                       -- regex match against worker.log error lines
  scope_pattern        TEXT,                       -- glob against affected files
  diagnosis            TEXT NOT NULL,
  recovery_action      TEXT NOT NULL CHECK (recovery_action IN (
    'cleaner-on-main','rebase-and-retry','switch-agent','shrink-scope',
    'escalate-human','wait-and-retry','mark-wontfix','no-op'
  )),
  recovery_args_json   TEXT NOT NULL DEFAULT '{}',
  source               TEXT NOT NULL CHECK (source IN ('llm','human','seed')),
  llm_provider         TEXT,
  llm_model            TEXT,
  success_count        INTEGER NOT NULL DEFAULT 0,
  failure_count        INTEGER NOT NULL DEFAULT 0,
  created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  retired_at           TEXT,
  retired_reason       TEXT
);

CREATE INDEX IF NOT EXISTS idx_lessons_bank_class
  ON lessons_bank(failure_class) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_lessons_bank_recovery
  ON lessons_bank(recovery_action) WHERE retired_at IS NULL;

-- ── mo_failure_fingerprints ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mo_failure_fingerprints (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint     TEXT NOT NULL UNIQUE,            -- sha256 of failure signature
  lesson_id       INTEGER NOT NULL REFERENCES lessons_bank(id),
  hit_count       INTEGER NOT NULL DEFAULT 0,
  last_hit_at     TEXT,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_failure_fingerprints_lesson
  ON mo_failure_fingerprints(lesson_id);

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0003_tickets_gauntlet.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'v1');
