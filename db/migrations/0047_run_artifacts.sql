-- 0047_run_artifacts.sql
-- run-artifacts-store (kickoff feat/run-artifacts-store): register every
-- persisted node artifact (.stream.jsonl, .transcript.json, evidence_bundle,
-- derived kinds) as a row keyed by run_id+node so cross-run audits can resolve
-- the path WITHOUT scanning the run dir. Additive only — no ALTER on existing
-- tables, no backfill.
--
-- Columns match the kickoff contract verbatim:
--   id, run_id, node_id, call_id, kind, rel_path, bytes, sha256, created_at
--
-- rel_path is ALWAYS relative to MINI_ORK_RUN_DIR (no leading '/', no '..').
-- Python writer (mini_ork/dispatch/telemetry.py:persist_artifact) rejects
-- absolute / parent-relative rel_paths with a soft warning so the convention
-- stays portable across vendor / foreign-home / cloud exec run dirs.
--
-- call_id is nullable on purpose: the bash mirror (lib/llm-dispatch.sh
-- _mo_llm_persist_agent_transcript) cannot cheaply recover llm_calls.lastrowid
-- across an inline-python boundary, so bash writes call_id=NULL. Python
-- dispatch (mini_ork/dispatch/providers.py) writes the real id once the
-- existing persist_call row is committed.
--
-- Idempotency: brand-new table, so CREATE TABLE IF NOT EXISTS +
-- CREATE INDEX IF NOT EXISTS is sufficient (mirrors 0043_lane_domain_advantage,
-- 0045_defect_attributions, 0046_semantic_memory). INSERT OR IGNORE INTO
-- schema_migrations at the bottom keeps `mini-ork init` re-runs stable.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS run_artifacts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id     TEXT    NOT NULL,
  node_id    TEXT,
  call_id    INTEGER,                          -- nullable: bash mirror leaves NULL
  kind       TEXT    NOT NULL,                 -- 'turn_jsonl' | 'transcript' | 'evidence_bundle' | ...
  rel_path   TEXT    NOT NULL,                 -- relative to MINI_ORK_RUN_DIR, no leading '/', no '..'
  bytes      INTEGER,
  sha256     TEXT,
  created_at INTEGER NOT NULL,                 -- unix epoch seconds
  UNIQUE(run_id, node_id, kind, rel_path)
);

-- Primary access pattern: list every artifact for a run (e.g. summarize a run).
CREATE INDEX IF NOT EXISTS idx_run_artifacts_run_id
  ON run_artifacts(run_id);

-- Secondary pattern: filter by kind within a run (e.g. all transcripts).
CREATE INDEX IF NOT EXISTS idx_run_artifacts_run_kind
  ON run_artifacts(run_id, kind);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0047_run_artifacts.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'run-artifacts-v1');