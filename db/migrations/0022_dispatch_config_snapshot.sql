-- 0022_dispatch_config_snapshot.sql - freeze lane attribution per task_run.
--
-- NULL means "legacy/pre-migration row"; readers should fall back to the
-- current config/agents.yaml fingerprint when no dispatch snapshot exists.

BEGIN;

ALTER TABLE task_runs
  ADD COLUMN dispatch_config_json TEXT NULL;

ALTER TABLE task_runs
  ADD COLUMN agents_yaml_sha TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_task_runs_agents_yaml_sha
  ON task_runs(agents_yaml_sha)
  WHERE agents_yaml_sha IS NOT NULL;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0022_dispatch_config_snapshot.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), '');

COMMIT;
