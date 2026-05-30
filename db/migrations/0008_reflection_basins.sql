-- mini-ork migration 0008 — reflection + decision basins + emergent patterns
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0008_reflection_basins.sql
BEGIN;

-- ── reflection_log ────────────────────────────────────────────────────────────
-- Tracks each time a reflection check was enqueued and whether the item drifted.
-- Consumed by the reflection-refiner stage and the cron sweeper.
CREATE TABLE IF NOT EXISTS reflection_log (
  log_id               INTEGER PRIMARY KEY AUTOINCREMENT,
  item_table           TEXT NOT NULL
                       CHECK (item_table IN (
                         'arch_specs','module_plans','atom_prs','adrs',
                         'node_annotations','communities','validations','fixes'
                       )),
  item_id              TEXT NOT NULL,
  trigger              TEXT NOT NULL CHECK (trigger IN ('commit','cron','read','manual')),
  trigger_sha          TEXT,
  enqueued_at          INTEGER NOT NULL,
  processed_at         INTEGER,
  outcome              TEXT,                       -- 'fresh'|'auto_recovered'|'needs_review'|'stale'
  drift_summary_json   TEXT,
  recovery_action      TEXT                        -- 'none'|'updated_path'|'updated_line'|'enqueued_redispatch'
);

CREATE INDEX IF NOT EXISTS idx_reflection_log_unprocessed
  ON reflection_log(processed_at) WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_reflection_log_item
  ON reflection_log(item_table, item_id);

-- ── decision_basins ───────────────────────────────────────────────────────────
-- Attractor basins that cluster related architectural decisions.
-- Importance decays over time; basin centroid summarised by LLM periodically.
CREATE TABLE IF NOT EXISTS decision_basins (
  basin_id             TEXT PRIMARY KEY,
  centroid_files_json  TEXT NOT NULL,              -- JSON array of files
  centroid_theme       TEXT,                       -- LLM-derived; null until summarized
  member_count         INTEGER NOT NULL DEFAULT 0,
  importance           REAL NOT NULL DEFAULT 1.0,  -- adaptive decay
  created_at           INTEGER NOT NULL,
  last_touched_at      INTEGER NOT NULL
);

-- ── decision_basin_membership ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS decision_basin_membership (
  basin_id             TEXT NOT NULL,
  item_table           TEXT NOT NULL,
  item_id              TEXT NOT NULL,
  joined_at            INTEGER NOT NULL,
  PRIMARY KEY (basin_id, item_table, item_id)
);

CREATE INDEX IF NOT EXISTS idx_decision_basin_membership_item
  ON decision_basin_membership(item_table, item_id);

-- ── emergent_patterns ─────────────────────────────────────────────────────────
-- Cross-feature patterns detected across multiple decision basins.
-- Each pattern may generate a suggested meta-ADR for human review.
CREATE TABLE IF NOT EXISTS emergent_patterns (
  pattern_id           TEXT PRIMARY KEY,
  cluster_label        TEXT NOT NULL,              -- LLM summary of shared theme
  member_item_ids_json TEXT NOT NULL,              -- JSON array of {item_table, item_id}
  feature_set_json     TEXT NOT NULL,              -- JSON array of feature names
  strength_score       REAL NOT NULL,
  suggested_meta_adr   TEXT,                       -- proposed meta-ADR markdown
  status               TEXT NOT NULL DEFAULT 'proposed'
                       CHECK (status IN ('proposed','approved','rejected','superseded')),
  detected_at          INTEGER NOT NULL,
  resolved_at          INTEGER
);

CREATE INDEX IF NOT EXISTS idx_emergent_patterns_status ON emergent_patterns(status);

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0008_reflection_basins.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'v1');
