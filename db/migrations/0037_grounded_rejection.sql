-- 0037_grounded_rejection.sql
-- HarnessBridge Technique 4 (arxiv:2606.12882):
-- Grounded rejection schema requiring (concern, evidence, suggestion) tuple
-- on every gate failure. Reflector reads structured rows instead of
-- re-extracting from prose; gradient quality compounds downstream.
--
-- Append-only by trigger, mirroring db/migrations/0036_safety_events.sql:30
-- and docs/SAFETY.md:116 conventions.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS grounded_rejections (
  id                  TEXT PRIMARY KEY,
  ts                  INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  run_id              TEXT,
  gate_name           TEXT NOT NULL,    -- coalition | krippendorff_alpha | citation_verifier | refute_or_promote | honest_ci
  verdict             TEXT NOT NULL CHECK (verdict IN ('fail','needs_revision','indeterminate')),
  concern             TEXT NOT NULL,    -- one sentence: why the proposal is unlikely to be productive
  evidence_trace_ids  TEXT NOT NULL,    -- JSON array of trace_id strings supporting the assessment
  evidence_summary    TEXT NOT NULL,    -- brief excerpt or pointer to the supporting span
  suggestion          TEXT NOT NULL,    -- actionable direction for revision
  consumed_by_reflector_ts INTEGER      -- set when reflector reads the row; null otherwise
);

.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; if sqlite3 \"$db\" \"SELECT COUNT(*) FROM pragma_table_info(\\\"grounded_rejections\\\") WHERE name = \\\"ts\\\";\" | grep -q \"^1$\"; then printf \"%s\n\" \"CREATE INDEX IF NOT EXISTS idx_grounded_rejections_ts\" \"  ON grounded_rejections(ts DESC);\" \"\" \"CREATE INDEX IF NOT EXISTS idx_grounded_rejections_run\" \"  ON grounded_rejections(run_id, ts DESC) WHERE run_id IS NOT NULL;\" \"\" \"CREATE INDEX IF NOT EXISTS idx_grounded_rejections_unconsumed\" \"  ON grounded_rejections(consumed_by_reflector_ts) WHERE consumed_by_reflector_ts IS NULL;\" \"\" \"CREATE TRIGGER IF NOT EXISTS grounded_rejections_no_immutable_update\" \"  BEFORE UPDATE OF id, ts, run_id, gate_name, verdict, concern, evidence_trace_ids, evidence_summary, suggestion\" \"  ON grounded_rejections\" \"  BEGIN\" \"    SELECT RAISE(ABORT, '\"'\"'grounded_rejections: provenance fields are immutable; only consumed_by_reflector_ts may be set'\"'\"');\" \"  END;\" \"\" \"CREATE TRIGGER IF NOT EXISTS grounded_rejections_no_delete\" \"  BEFORE DELETE ON grounded_rejections\" \"  BEGIN\" \"    SELECT RAISE(ABORT, '\"'\"'grounded_rejections is append-only'\"'\"');\" \"  END;\"; fi'"

PRAGMA foreign_keys = ON;
