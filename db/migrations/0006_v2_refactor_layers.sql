-- mini-ork migration 0006 — V2 refactor layers (arch specs, module plans, atom PRs, ADRs)
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0006_v2_refactor_layers.sql
BEGIN;

-- ── arch_specs ────────────────────────────────────────────────────────────────
-- Hoare-triple specifications produced by the architecture stage of mini-orch.
-- Each arch_spec states a precondition P, postcondition Q, and a verifier command.
CREATE TABLE IF NOT EXISTS arch_specs (
  arch_id              TEXT PRIMARY KEY,           -- e.g. 'ARCH-1'
  feature              TEXT NOT NULL,
  cycle_id             TEXT NOT NULL,
  title                TEXT NOT NULL,
  precondition         TEXT NOT NULL,              -- Hoare P
  postcondition        TEXT NOT NULL,              -- Hoare Q
  frame_json           TEXT,                       -- JSON array of files NOT touched (sep-logic frame)
  info_gain            REAL,                       -- MDL-based ranking score
  verifier             TEXT NOT NULL,              -- shell command that checks Q
  evidence_for_pre     TEXT,                       -- JSON array of {file, line} citations
  status               TEXT NOT NULL DEFAULT 'proposed'
                       CHECK (status IN ('proposed','accepted','shipped','deprecated')),
  adr_id               TEXT,                       -- logical FK to adrs(adr_id)
  -- reflection columns
  via_gate             TEXT NOT NULL DEFAULT 'architectural_decision_gate',
  reflection_at        INTEGER NOT NULL,
  reflection_sha       TEXT,
  reflected_substrate  TEXT,                       -- JSON snapshot (cited_files w/ XXH3 + fingerprints)
  reflection_status    TEXT NOT NULL DEFAULT 'fresh'
                       CHECK (reflection_status IN ('fresh','stale','drifted','auto_recovered','gone','needs_review','deprecated')),
  reflection_last_check INTEGER,
  reflection_drift_log TEXT DEFAULT '[]',          -- JSON array of drift events
  created_at           INTEGER NOT NULL,
  updated_at           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_arch_specs_feature_cycle ON arch_specs(feature, cycle_id);
CREATE INDEX IF NOT EXISTS idx_arch_specs_status        ON arch_specs(status);
CREATE INDEX IF NOT EXISTS idx_arch_specs_reflection    ON arch_specs(reflection_status);

-- ── module_plans ──────────────────────────────────────────────────────────────
-- Candidate refactor plans for each arch spec. Typically 3 candidates per spec
-- (max cohesion, min churn, balanced). The recommended one gets is_recommended=1.
CREATE TABLE IF NOT EXISTS module_plans (
  module_id            TEXT NOT NULL,              -- e.g. 'M1'
  candidate_id         TEXT NOT NULL,              -- e.g. 'M1-A'|'M1-B'|'M1-C'
  arch_id              TEXT NOT NULL,              -- logical FK to arch_specs
  cycle_id             TEXT NOT NULL,
  label                TEXT NOT NULL,              -- 'max cohesion'|'min churn'|'balanced'
  files_touched        INTEGER,
  new_files_json       TEXT,                       -- JSON array
  files_deleted        INTEGER,
  cohesion_score       REAL,
  coupling_score       REAL,
  files_touched_score  REAL,
  volatility_score     REAL,
  frame_json           TEXT,
  is_recommended       INTEGER DEFAULT 0,
  status               TEXT NOT NULL DEFAULT 'proposed',
  -- reflection
  via_gate             TEXT NOT NULL DEFAULT 'architectural_decision_gate',
  reflection_at        INTEGER NOT NULL,
  reflection_sha       TEXT,
  reflected_substrate  TEXT,
  reflection_status    TEXT NOT NULL DEFAULT 'fresh',
  reflection_last_check INTEGER,
  reflection_drift_log TEXT DEFAULT '[]',
  created_at           INTEGER NOT NULL,
  PRIMARY KEY (module_id, candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_module_plans_arch        ON module_plans(arch_id);
CREATE INDEX IF NOT EXISTS idx_module_plans_cycle       ON module_plans(cycle_id);
CREATE INDEX IF NOT EXISTS idx_module_plans_recommended ON module_plans(is_recommended);

-- ── atom_prs ──────────────────────────────────────────────────────────────────
-- Atomic pull-request specifications derived from module_plans.
-- Each atom_pr targets exactly one refactor kind and is independently reviewable.
CREATE TABLE IF NOT EXISTS atom_prs (
  pr_id                TEXT PRIMARY KEY,           -- e.g. 'ATOM-PR-1'
  module_id            TEXT NOT NULL,
  candidate_id         TEXT,
  cycle_id             TEXT NOT NULL,
  title                TEXT NOT NULL,
  kind                 TEXT NOT NULL CHECK (kind IN (
                         'rename','extract','inline','signature_change','delete','wire'
                       )),
  frame_json           TEXT,
  depends_on_json      TEXT,                       -- JSON array of pr_ids
  test_gate            TEXT NOT NULL,              -- shell command that must pass
  functoriality_check  TEXT,                       -- shell command verifying call-graph preserved
  status               TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','shipped','reverted')),
  commit_sha           TEXT,
  -- reflection
  via_gate             TEXT NOT NULL DEFAULT 'artifact_committed_gate',
  reflection_at        INTEGER NOT NULL,
  reflection_sha       TEXT,
  reflected_substrate  TEXT,
  reflection_status    TEXT NOT NULL DEFAULT 'fresh',
  reflection_last_check INTEGER,
  reflection_drift_log TEXT DEFAULT '[]',
  created_at           INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_atom_prs_module ON atom_prs(module_id);
CREATE INDEX IF NOT EXISTS idx_atom_prs_status ON atom_prs(status);

-- ── adrs ──────────────────────────────────────────────────────────────────────
-- Architecture Decision Records: accepted, deprecated, or superseded decisions.
CREATE TABLE IF NOT EXISTS adrs (
  adr_id               TEXT PRIMARY KEY,           -- e.g. 'ADR-24'
  arch_id              TEXT,                       -- logical FK to arch_specs
  title                TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'accepted'
                       CHECK (status IN ('accepted','deprecated','superseded')),
  supersedes           TEXT,                       -- logical FK to another adr_id
  replaced_by          TEXT,
  precondition         TEXT NOT NULL,
  postcondition        TEXT NOT NULL,
  verifier             TEXT NOT NULL,              -- shell command, mechanically checkable
  body_md              TEXT NOT NULL,
  -- reflection
  via_gate             TEXT NOT NULL DEFAULT 'architectural_decision_gate',
  reflection_at        INTEGER NOT NULL,
  reflection_sha       TEXT,
  reflected_substrate  TEXT,
  reflection_status    TEXT NOT NULL DEFAULT 'fresh',
  reflection_last_check INTEGER,
  reflection_drift_log TEXT DEFAULT '[]',
  written_at           INTEGER NOT NULL,
  written_by_cycle     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_adrs_status ON adrs(status);
CREATE INDEX IF NOT EXISTS idx_adrs_arch   ON adrs(arch_id);

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0006_v2_refactor_layers.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'v1');
