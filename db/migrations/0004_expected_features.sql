-- mini-ork migration 0004 — expected features + consensus pipeline
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0004_expected_features.sql
BEGIN;

-- ── expected_features ─────────────────────────────────────────────────────────
-- Canonical registry of features that SHOULD exist in the product.
-- Populated by design extractors; consumed by gauntlet probes and ticket emitters.
CREATE TABLE IF NOT EXISTS expected_features (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  route             TEXT NOT NULL,                 -- URL path for the feature, e.g. '/dashboard'
  slug              TEXT NOT NULL,                 -- stable identifier, e.g. 'search-bar'
  title             TEXT NOT NULL,
  description       TEXT,
  design_source     TEXT,                          -- design_source: path to design artifact, project-defined
  expected_testids       TEXT NOT NULL DEFAULT '[]',  -- JSON array of strings
  expected_interactions  TEXT NOT NULL DEFAULT '[]',  -- JSON array of {action, outcome}
  expected_states        TEXT NOT NULL DEFAULT '[]',  -- JSON array: ['empty','loading','error']
  ignore_regions         TEXT NOT NULL DEFAULT '[]',  -- JSON array of CSS selectors / xpath for validator masks
  fingerprint            TEXT NOT NULL,            -- sha1(route|slug|description) for dedup
  source_screenshot      TEXT,                     -- abs path to design-source screenshot
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  archived_at       TEXT,
  source_extractors TEXT NOT NULL DEFAULT '["visual"]',
  parent_slug       TEXT,
  section_order     INTEGER DEFAULT 0,
  tier              TEXT CHECK (tier IN ('MUST','SHOULD','COULD') OR tier IS NULL),
  status            TEXT CHECK (status IN ('missing','partial','implemented','skipped','deprecated','proposed') OR status IS NULL) DEFAULT 'missing',
  status_evidence   TEXT,
  status_updated_at TEXT,
  journey_step_id   TEXT,                          -- → journey_steps(id) (table in 0005)
  UNIQUE(route, slug),
  UNIQUE(fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_expected_features_route
  ON expected_features(route) WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_expected_features_design
  ON expected_features(design_source);
CREATE INDEX IF NOT EXISTS idx_ef_tree
  ON expected_features(route, parent_slug, section_order) WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ef_status
  ON expected_features(status, route) WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ef_journey_step
  ON expected_features(journey_step_id);

CREATE TRIGGER IF NOT EXISTS trg_expected_features_updated
AFTER UPDATE ON expected_features
BEGIN
  UPDATE expected_features SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_expected_features_fingerprint_immutable
BEFORE UPDATE OF fingerprint ON expected_features
FOR EACH ROW
WHEN OLD.fingerprint IS NOT NULL
  AND NEW.fingerprint IS NOT NULL
  AND NEW.fingerprint != OLD.fingerprint
BEGIN
  SELECT RAISE(ABORT, 'expected_features.fingerprint is immutable post-insert.');
END;

-- ── expected_features_proposed ────────────────────────────────────────────────
-- Staging table for features proposed by visual or brief extractors.
-- Feature consensus logic merges these into expected_features.
CREATE TABLE IF NOT EXISTS expected_features_proposed (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  source            TEXT NOT NULL CHECK(source IN ('visual','brief')),
  route             TEXT NOT NULL,
  slug              TEXT NOT NULL,
  title             TEXT NOT NULL,
  description       TEXT,
  design_source     TEXT,
  expected_testids       TEXT NOT NULL DEFAULT '[]',
  expected_interactions  TEXT NOT NULL DEFAULT '[]',
  expected_states        TEXT NOT NULL DEFAULT '[]',
  ignore_regions         TEXT NOT NULL DEFAULT '[]',
  source_screenshot      TEXT,
  fingerprint            TEXT NOT NULL,
  proposed_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  consumed_at       TEXT,                          -- set when merged into expected_features
  UNIQUE(source, route, slug)
);

CREATE INDEX IF NOT EXISTS idx_efp_unconsumed
  ON expected_features_proposed(route, source) WHERE consumed_at IS NULL;

-- ── feature_consensus_log ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS feature_consensus_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  route             TEXT NOT NULL,
  decision          TEXT NOT NULL CHECK(decision IN ('merged','accepted_visual_only','accepted_brief_only','rejected','needs_redesign')),
  visual_proposal_id  INTEGER,                     -- expected_features_proposed.id
  brief_proposal_id   INTEGER,                     -- expected_features_proposed.id
  merged_feature_id   INTEGER,                     -- expected_features.id (NULL for rejected)
  similarity        REAL,                          -- cosine 0..1, or NULL for *_ONLY cases
  rationale         TEXT,
  cost_usd          REAL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_fcl_route ON feature_consensus_log(route, ts);

-- ── proposed_epics ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS proposed_epics (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  proposed_id     TEXT UNIQUE NOT NULL,            -- "PE-001" auto-incremented
  title           TEXT NOT NULL,
  rationale       TEXT NOT NULL,
  scope           TEXT NOT NULL,
  estimated_days  REAL,
  proposed_lane   TEXT,
  source          TEXT NOT NULL DEFAULT 'pm-proposer', -- 'pm-proposer' | 'human'
  evidence        TEXT,                            -- JSON: {gap_routes:[], missing_components:[], old_only:[]}
  related_tickets TEXT,                            -- comma-list of ticket_ids subsumed
  status          TEXT NOT NULL DEFAULT 'pending_review'
                  CHECK (status IN ('pending_review','accepted','rejected','duplicate')),
  reviewed_by     TEXT,
  reviewed_at     TEXT,
  promoted_epic_id TEXT,                           -- when accepted, the new epics.id
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_proposed_epics_status ON proposed_epics(status);

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0004_expected_features.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'v1');
