-- 0049_lane_advantage_variance.sql
--
-- Cost-free contextual-bandit upgrade to the lane router. ADDITIVE only.
-- Existing tables and their columns stay byte-compatible; this migration
-- introduces a new persistence slice (lane_slice_baseline) and stores
-- per-lane advantage variance/std columns that the bandit uses to score
-- lanes. The UCB selector (D1) and z-score normaliser (D3) read these new
-- columns ONLY when MO_ROUTER_UCB_C > 0; MO_ROUTER_UCB_C=0 leaves the
-- legacy within-group-mean path completely intact (no reads of new columns
-- in the selector, no value changes to relative_advantage columns).
--
-- D1: advantage_var / advantage_std on lane_domain_advantage +
--     lane_region_advantage so the UCB bonus can be computed.
-- D2: lane_slice_baseline — per-slice persistent EMA baseline used by the
--     single-sample fallback (MO_ROUTER_SINGLE_SAMPLE=1) and as the
--     empirical-Bayes prior mean for the z-score normaliser (D3).
-- D4: route_source / route_explore / route_score on execution_traces —
--     SCHEMA ONLY in this migration. The decision-service writer that
--     populates them and scripts/router_replay_eval.py that reads them are a
--     follow-up (not yet implemented); these columns stay NULL until then.
--
-- Discipline: ALL idempotent. CREATE TABLE/INDEX IF NOT EXISTS for new
-- tables, ALTER TABLE ADD COLUMN for existing tables. No DROP / DELETE.
-- agent_performance_memory is INTENTIONALLY untouched — the global slice
-- stays on within-group means; variance/std columns would force every
-- existing reader off the byte-equivalence path on default UCB_C=0.

PRAGMA foreign_keys = OFF;

-- ── lane_slice_baseline ─────────────────────────────────────────────────────
-- One row per slice keyed by (objective_domain, task_class, node_type,
-- code_region). Stores the slice-wide recency-EMA baseline of reward_g and
-- its standard deviation. Used as the empirical-Bayes prior mean (D3) AND as
-- the destination for single-sample (1-member group) advantage writes when
-- MO_ROUTER_SINGLE_SAMPLE=1 (D2).
--
-- We deliberately do NOT key this by lane: a baseline is a population mean,
-- not a per-lane mean. Per-lane variance lives in lane_domain_advantage.
CREATE TABLE IF NOT EXISTS lane_slice_baseline (
  objective_domain   TEXT    NOT NULL DEFAULT '',
  task_class         TEXT    NOT NULL,
  node_type          TEXT    NOT NULL DEFAULT '',
  code_region        TEXT    NOT NULL DEFAULT '',
  slice_mean         REAL    NOT NULL DEFAULT 0.0,
  slice_var          REAL    NOT NULL DEFAULT 0.0,
  slice_std          REAL    NOT NULL DEFAULT 0.0,
  runs_count         INTEGER NOT NULL DEFAULT 0,
  last_updated       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (objective_domain, task_class, node_type, code_region)
);

CREATE INDEX IF NOT EXISTS idx_lane_slice_baseline_lookup
  ON lane_slice_baseline(task_class, node_type, objective_domain);

-- ── advantage_var / advantage_std on per-domain + region slices ────────────
-- Per-lane variance / std of reward_g within the slice's group window.
-- Populated by recompute_advantages whenever MO_ROUTER_UCB_C > 0 OR
-- MO_ROUTER_SINGLE_SAMPLE > 0; NOT populated on the all-flags-off path so
-- the legacy within-group relative_advantage byte-equivalence holds.
--
-- lane_domain_advantage exists from 0043; lane_region_advantage is created
-- LAZILY at runtime by recompute_advantages, so at migration time it may not
-- exist yet. Create both here (mirroring the runtime schema, including the new
-- bandit columns) so the ALTER shim below can never hit a missing table. All
-- IF NOT EXISTS → no-op where the table already exists.
-- Base schema only (matching 0043 + the runtime CREATE): the bandit columns
-- are added by the ALTER shim below. Do NOT list advantage_var/std here or the
-- shim (which runs on a separate connection that can't see this uncommitted
-- CREATE) will emit a duplicate ALTER.
CREATE TABLE IF NOT EXISTS lane_domain_advantage (
  agent_version_id   TEXT    NOT NULL,
  task_class         TEXT    NOT NULL,
  node_type          TEXT    NOT NULL DEFAULT '',
  objective_domain   TEXT    NOT NULL DEFAULT '',
  relative_advantage REAL    NOT NULL DEFAULT 0.0,
  runs_count         INTEGER NOT NULL DEFAULT 0,
  success_count      INTEGER NOT NULL DEFAULT 0,
  last_updated       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (agent_version_id, task_class, node_type, objective_domain)
);
CREATE TABLE IF NOT EXISTS lane_region_advantage (
  agent_version_id   TEXT    NOT NULL,
  task_class         TEXT    NOT NULL,
  node_type          TEXT    NOT NULL DEFAULT '',
  objective_domain   TEXT    NOT NULL DEFAULT '',
  code_region        TEXT    NOT NULL DEFAULT '',
  relative_advantage REAL    NOT NULL DEFAULT 0.0,
  runs_count         INTEGER NOT NULL DEFAULT 0,
  success_count      INTEGER NOT NULL DEFAULT 0,
  last_updated       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (agent_version_id, task_class, node_type, objective_domain, code_region)
);

-- The `_read` shim inside sqlite prevents an ADD COLUMN from failing on
-- already-applied DBs (idempotent apply across the migration graph).
.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; for tbl in lane_domain_advantage lane_region_advantage; do for col in advantage_var advantage_std z_score_advantage; do have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"$tbl\\\") WHERE name = \\\"$col\\\";\"); if [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE $tbl ADD COLUMN $col REAL NOT NULL DEFAULT 0.0;\"; fi; done; done'"

-- ── execution_traces propensity columns (D4) ────────────────────────────────
-- Nullable: only routed nodes (executor-dispatched, not framework-internal
-- traces) populate these. Pre-existing execution_traces rows stay NULL.
.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; for col_spec in \"route_source TEXT DEFAULT NULL\" \"route_explore INTEGER DEFAULT NULL\" \"route_score REAL DEFAULT NULL\"; do col=${col_spec%% *}; have=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"execution_traces\\\") WHERE name = \\\"$col\\\";\"); if [ \"${have:-0}\" = \"0\" ]; then printf \"%s\n\" \"ALTER TABLE execution_traces ADD COLUMN $col_spec;\"; fi; done'"

-- ── propensity index (reserved for the D6 replay-eval follow-up) ────────────
CREATE INDEX IF NOT EXISTS idx_et_route_source
  ON execution_traces(route_source) WHERE route_source IS NOT NULL;

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0049_lane_advantage_variance.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'lane-advantage-variance-v1');
