-- 0023_node_heartbeat_fuse.sql - node heartbeat liveness + lane fuse state.
--
-- NULL heartbeat values mean "legacy/pre-migration row". Fuse columns are
-- backward-compatible: a NULL fuse_blown_lane means no fuse has halted the run,
-- and fuse_consecutive_failures starts at 0 for old and new task_runs.

BEGIN;

ALTER TABLE run_events
  ADD COLUMN last_heartbeat_at INTEGER NULL;

ALTER TABLE task_runs
  ADD COLUMN fuse_blown_lane TEXT NULL;

ALTER TABLE task_runs
  ADD COLUMN fuse_consecutive_failures INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_run_events_last_heartbeat_at
  ON run_events(last_heartbeat_at)
  WHERE last_heartbeat_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_task_runs_fuse_blown_lane
  ON task_runs(fuse_blown_lane)
  WHERE fuse_blown_lane IS NOT NULL;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0023_node_heartbeat_fuse.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), '');

COMMIT;
