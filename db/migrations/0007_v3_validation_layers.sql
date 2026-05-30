-- mini-ork migration 0007 — V3 validation layers
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0007_v3_validation_layers.sql
BEGIN;

-- ── node_annotations ─────────────────────────────────────────────────────────
-- DSAP (Dynamic State Annotation Protocol) annotations on code nodes.
-- Each annotation captures precondition P, postcondition Q, callers/callees,
-- and mutation weight for the SABER scoring system.
CREATE TABLE IF NOT EXISTS node_annotations (
  node_id              TEXT PRIMARY KEY,           -- 'fn:path/to/file.ts:symbolName'
  file_path            TEXT NOT NULL,
  symbol_name          TEXT NOT NULL,
  content_hash         TEXT NOT NULL,              -- XXH3 of source — cache key
  task                 TEXT NOT NULL,
  pre_state_json       TEXT NOT NULL,              -- DSAP P
  post_state_json      TEXT NOT NULL,              -- DSAP Q
  guard                TEXT,                       -- shell/code for tri-state check
  frame_json           TEXT,
  mutating             INTEGER NOT NULL DEFAULT 0, -- 0|1 — SABER mutation weight
  side_effects_json    TEXT,                       -- JSON array
  callers_json         TEXT,                       -- JSON array of node_ids
  callees_json         TEXT,
  annotated_at         INTEGER NOT NULL,
  annotated_by_cycle   TEXT NOT NULL,
  -- reflection
  via_gate             TEXT NOT NULL DEFAULT 'verifier_run_gate',
  reflection_at        INTEGER NOT NULL,
  reflection_sha       TEXT,
  reflected_substrate  TEXT,
  reflection_status    TEXT NOT NULL DEFAULT 'fresh',
  reflection_last_check INTEGER,
  reflection_drift_log TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_node_annotations_file     ON node_annotations(file_path);
CREATE INDEX IF NOT EXISTS idx_node_annotations_hash     ON node_annotations(content_hash);
CREATE INDEX IF NOT EXISTS idx_node_annotations_mutating ON node_annotations(mutating);

-- ── communities ───────────────────────────────────────────────────────────────
-- Louvain community clusters of node_annotations. Each community is a
-- co-mutation hotspot scored for validation priority.
CREATE TABLE IF NOT EXISTS communities (
  community_id         TEXT PRIMARY KEY,
  feature              TEXT NOT NULL,
  cycle_id             TEXT NOT NULL,
  member_node_ids      TEXT NOT NULL,              -- JSON array of node_ids
  mutation_density     REAL,
  recent_failure_rate  REAL,
  hub_centrality       REAL,
  coverage_gap         REAL,
  score                REAL,
  rank                 INTEGER,
  detected_at          INTEGER NOT NULL,
  invalidated_at       INTEGER,
  invalidated_by       TEXT                        -- logical FK to validations(validation_id)
);

CREATE INDEX IF NOT EXISTS idx_communities_feature_rank ON communities(feature, rank);
CREATE INDEX IF NOT EXISTS idx_communities_invalidated  ON communities(invalidated_at);

-- ── validations ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS validations (
  validation_id        TEXT PRIMARY KEY,
  route_path           TEXT NOT NULL,              -- e.g. 'POST /api/.../resume'
  community_id         TEXT NOT NULL,
  node_ids_json        TEXT NOT NULL,              -- JSON array of nodes on route
  verdict              TEXT NOT NULL CHECK (verdict IN ('pass','retry','fatal')),
  bugs_json            TEXT,                       -- JSON array of {node, violation, evidence, fix_suggestion}
  evidence_files_json  TEXT,                       -- JSON array (log paths, curl outputs)
  evidence_sha         TEXT,                       -- git HEAD at validation time
  validated_at         INTEGER NOT NULL,
  cycle_id             TEXT NOT NULL,
  -- reflection
  via_gate             TEXT NOT NULL DEFAULT 'verifier_run_gate',
  reflection_at        INTEGER NOT NULL,
  reflection_sha       TEXT,
  reflected_substrate  TEXT,
  reflection_status    TEXT NOT NULL DEFAULT 'fresh',
  reflection_last_check INTEGER,
  reflection_drift_log TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_validations_route     ON validations(route_path);
CREATE INDEX IF NOT EXISTS idx_validations_community ON validations(community_id);
CREATE INDEX IF NOT EXISTS idx_validations_verdict   ON validations(verdict);

-- ── fixes ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fixes (
  fix_id               TEXT PRIMARY KEY,
  validation_id        TEXT NOT NULL,              -- logical FK to validations
  patch                TEXT NOT NULL,              -- unified diff
  frame_check          TEXT NOT NULL CHECK (frame_check IN ('pass','fail')),
  functoriality_check  TEXT NOT NULL CHECK (functoriality_check IN ('pass','fail','skipped')),
  test_result          TEXT CHECK (test_result IN ('pass','fail','skipped') OR test_result IS NULL),
  commit_sha           TEXT,
  shipped_at           INTEGER,
  rolled_back_at       INTEGER,
  -- reflection
  via_gate             TEXT NOT NULL DEFAULT 'fix_attempt_gate',
  reflection_at        INTEGER NOT NULL,
  reflection_sha       TEXT,
  reflected_substrate  TEXT,
  reflection_status    TEXT NOT NULL DEFAULT 'fresh',
  reflection_last_check INTEGER,
  reflection_drift_log TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_fixes_validation ON fixes(validation_id);
CREATE INDEX IF NOT EXISTS idx_fixes_commit     ON fixes(commit_sha);

-- ── cascade_invalidations ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cascade_invalidations (
  source_validation_id     TEXT NOT NULL,
  invalidated_community_id TEXT NOT NULL,
  reason                   TEXT NOT NULL,          -- e.g. 'shared_node_modified'
  shared_nodes_json        TEXT NOT NULL,
  invalidated_at           INTEGER NOT NULL,
  PRIMARY KEY (source_validation_id, invalidated_community_id)
);

-- ── inspector_runs ────────────────────────────────────────────────────────────
-- Dual-inspector consensus runs (opus + codex) for stage gate validation.
CREATE TABLE IF NOT EXISTS inspector_runs (
  inspector_run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  site                 TEXT NOT NULL,              -- 'stage1_consensus'|'stage2_consensus'|'stage4_adr_validation'|'layer3_verdict_review'|'meta_adr_summarization'
  cycle_id             TEXT NOT NULL,
  prompt_hash          TEXT NOT NULL,              -- XXH3 of prompt sent to both inspectors
  opus_verdict_json    TEXT,
  codex_verdict_json   TEXT,
  opus_rc              INTEGER,                    -- 0 = success
  codex_rc             INTEGER,
  agreement            INTEGER NOT NULL CHECK (agreement IN (0,1)),
  actions_diff_json    TEXT,                       -- non-empty when verdicts agree but actions differ
  final_verdict_json   TEXT,
  fallback_reason      TEXT,                       -- 'single_inspector_fallback'|'verdict_mismatch_paused'|NULL
  duration_ms_opus     INTEGER,
  duration_ms_codex    INTEGER,
  ran_at               INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inspector_runs_site_cycle ON inspector_runs(site, cycle_id);
CREATE INDEX IF NOT EXISTS idx_inspector_runs_agreement  ON inspector_runs(agreement);

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0007_v3_validation_layers.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'v1');
