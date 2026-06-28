-- 0045_defect_attributions.sql
-- frc-a3 (Track A3): storage prerequisite for blame attribution.
-- Records each (found_run_id, blamed_run_id) pair observed during a run,
-- along with the dispatching lane, code region, task class, and a
-- decaying penalty used to fold delayed defects back into the lane router
-- (Tracks A4 + A5).
--
-- Columns match the kickoff contract verbatim:
--   found_run_id, blamed_run_id, lane, code_region, task_class,
--   severity, penalty, decay_halflife_days, ts
--
-- Idempotency: brand-new table, so CREATE TABLE IF NOT EXISTS +
-- CREATE INDEX IF NOT EXISTS is sufficient (mirrors 0043_lane_domain_advantage).
-- The INSERT OR IGNORE INTO schema_migrations at the bottom keeps
-- `mini-ork init` re-runs stable.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS defect_attributions (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,

  -- Provenance
  found_run_id         TEXT    NOT NULL,
  blamed_run_id        TEXT    NOT NULL,
  lane                 TEXT    NOT NULL,
  code_region          TEXT    NOT NULL,
  task_class           TEXT    NOT NULL,

  -- Severity + decaying penalty (consumed by lib/blame_attributor.sh in A4)
  severity             TEXT    NOT NULL DEFAULT 'medium'
                         CHECK (severity IN ('low','medium','high','critical')),
  penalty              REAL    NOT NULL,                 -- typically negative; range [-1, 0]
  decay_halflife_days  REAL    NOT NULL DEFAULT 30.0,    -- half-life in days

  -- Timestamp (ISO-8601 UTC, matches core lifecycle tables)
  ts                   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Lookup index: primary access pattern is "show me recent penalties for
-- (lane, code_region, task_class)", e.g. when recomputing
-- lane_router_recompute_advantages in A5.
CREATE INDEX IF NOT EXISTS idx_defect_attributions_lookup
  ON defect_attributions(lane, code_region, task_class);

-- Pair index: dedupe + per-pair history scans.
CREATE INDEX IF NOT EXISTS idx_defect_attributions_pair
  ON defect_attributions(found_run_id, blamed_run_id);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0045_defect_attributions.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'defect-attributions-v1');