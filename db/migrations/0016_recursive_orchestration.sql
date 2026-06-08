-- 0016_recursive_orchestration.sql — bounded recursive mini-ork lineage
--
-- A parent task_run may delegate bounded child runs. Children write to the
-- same state.db but execute in isolated child workspaces under:
--   .mini-ork/runs/<parent-run>/children/<child-run>/
--
-- The parent remains responsible for merge/publish decisions.

BEGIN;

CREATE TABLE IF NOT EXISTS run_spawns (
  spawn_id             TEXT PRIMARY KEY,
  parent_run_id        TEXT NOT NULL,
  child_run_id         TEXT NOT NULL UNIQUE,
  root_run_id          TEXT NOT NULL,
  depth                INTEGER NOT NULL DEFAULT 1 CHECK (depth >= 1),
  recipe               TEXT,
  kickoff_path         TEXT NOT NULL,
  child_workspace      TEXT NOT NULL,
  authority_level      REAL NOT NULL DEFAULT 0.3 CHECK (authority_level >= 0.0 AND authority_level <= 1.0),
  allow_child_spawn    INTEGER NOT NULL DEFAULT 0 CHECK (allow_child_spawn IN (0,1)),
  status               TEXT NOT NULL DEFAULT 'approved'
                       CHECK (status IN ('requested','approved','running','completed','failed','blocked','merged','rejected')),
  policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
  created_at           INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  updated_at           INTEGER NOT NULL DEFAULT (strftime('%s','now')),

  FOREIGN KEY(parent_run_id) REFERENCES task_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_run_spawns_parent ON run_spawns(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_run_spawns_root ON run_spawns(root_run_id);
CREATE INDEX IF NOT EXISTS idx_run_spawns_status ON run_spawns(status);
CREATE INDEX IF NOT EXISTS idx_run_spawns_depth ON run_spawns(root_run_id, depth);

CREATE TABLE IF NOT EXISTS run_events (
  event_id       TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL,
  parent_run_id  TEXT,
  event_type     TEXT NOT NULL,
  payload_json   TEXT NOT NULL DEFAULT '{}',
  created_at     INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_run_events_parent ON run_events(parent_run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_run_events_type ON run_events(event_type, created_at);

CREATE TABLE IF NOT EXISTS run_artifact_edges (
  edge_id             TEXT PRIMARY KEY,
  producer_run_id    TEXT NOT NULL,
  consumer_run_id    TEXT NOT NULL,
  artifact_path      TEXT NOT NULL,
  artifact_hash      TEXT,
  artifact_kind      TEXT NOT NULL DEFAULT 'file',
  verification_state TEXT NOT NULL DEFAULT 'proposed'
                     CHECK (verification_state IN ('proposed','verified','rejected','merged')),
  created_at         INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_run_artifact_edges_producer ON run_artifact_edges(producer_run_id);
CREATE INDEX IF NOT EXISTS idx_run_artifact_edges_consumer ON run_artifact_edges(consumer_run_id);
CREATE INDEX IF NOT EXISTS idx_run_artifact_edges_state ON run_artifact_edges(verification_state);

CREATE TABLE IF NOT EXISTS merge_decisions (
  decision_id        TEXT PRIMARY KEY,
  parent_run_id      TEXT NOT NULL,
  child_run_id       TEXT NOT NULL,
  decision           TEXT NOT NULL CHECK (decision IN ('accepted','rejected','needs_changes','deferred')),
  reason             TEXT NOT NULL DEFAULT '',
  decided_by         TEXT NOT NULL DEFAULT 'parent',
  evidence_json      TEXT NOT NULL DEFAULT '{}',
  created_at         INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_merge_decisions_parent ON merge_decisions(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_merge_decisions_child ON merge_decisions(child_run_id);
CREATE INDEX IF NOT EXISTS idx_merge_decisions_decision ON merge_decisions(decision);

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0016_recursive_orchestration.sql', strftime('%s','now'), 'recursive-orchestration-v1');

COMMIT;
