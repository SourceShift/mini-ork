-- 0041_grounded_rejections_reconcile.sql
-- Reconcile copied live databases that may still contain the superseded
-- 0040 grounded_rejections shape (a table WITHOUT the canonical `ts` column).
-- review-37 HIGH: the previous version left a *non-empty* stale table
-- untouched AND skipped creating the canonical schema, so on those DBs the
-- canonical 0037 columns/indexes/triggers never existed and every canonical
-- insert/consumer failed.
--
-- Fixed convergence (no data loss, idempotent):
--   * stale + empty     → DROP the stale table.
--   * stale + non-empty → RENAME it to grounded_rejections_legacy_0040
--                         (rows preserved for forensic recovery).
--   * then ALWAYS (re)create the canonical 0037 table + indexes + triggers
--     with IF NOT EXISTS, so a canonical/absent DB converges and an
--     already-canonical DB is a no-op.

PRAGMA foreign_keys = OFF;

.read "|sh -c 'db=\"${MINI_ORK_DB:?}\"; stale=$(sqlite3 \"$db\" \"SELECT CASE WHEN EXISTS (SELECT 1 FROM sqlite_master WHERE type = \\\"table\\\" AND name = \\\"grounded_rejections\\\") AND NOT EXISTS (SELECT 1 FROM pragma_table_info(\\\"grounded_rejections\\\") WHERE name = \\\"ts\\\") THEN 1 ELSE 0 END;\"); rows=0; if [ \"$stale\" = \"1\" ]; then rows=$(sqlite3 \"$db\" \"SELECT COUNT(*) FROM grounded_rejections;\"); fi; if [ \"$stale\" = \"1\" ] && [ \"${rows:-0}\" = \"0\" ]; then printf \"%s\n\" \"DROP TABLE grounded_rejections;\"; elif [ \"$stale\" = \"1\" ]; then printf \"%s\n\" \"ALTER TABLE grounded_rejections RENAME TO grounded_rejections_legacy_0040;\"; fi; printf \"%s\n\" \"CREATE TABLE IF NOT EXISTS grounded_rejections (\" \"  id                  TEXT PRIMARY KEY,\" \"  ts                  INTEGER NOT NULL DEFAULT (strftime('\"'\"'%s'\"'\"','\"'\"'now'\"'\"')),\" \"  run_id              TEXT,\" \"  gate_name           TEXT NOT NULL,\" \"  verdict             TEXT NOT NULL CHECK (verdict IN ('\"'\"'fail'\"'\"','\"'\"'needs_revision'\"'\"','\"'\"'indeterminate'\"'\"')),\" \"  concern             TEXT NOT NULL,\" \"  evidence_trace_ids  TEXT NOT NULL,\" \"  evidence_summary    TEXT NOT NULL,\" \"  suggestion          TEXT NOT NULL,\" \"  consumed_by_reflector_ts INTEGER\" \");\" \"\" \"CREATE INDEX IF NOT EXISTS idx_grounded_rejections_ts\" \"  ON grounded_rejections(ts DESC);\" \"\" \"CREATE INDEX IF NOT EXISTS idx_grounded_rejections_run\" \"  ON grounded_rejections(run_id, ts DESC) WHERE run_id IS NOT NULL;\" \"\" \"CREATE INDEX IF NOT EXISTS idx_grounded_rejections_unconsumed\" \"  ON grounded_rejections(consumed_by_reflector_ts) WHERE consumed_by_reflector_ts IS NULL;\" \"\" \"CREATE TRIGGER IF NOT EXISTS grounded_rejections_no_immutable_update\" \"  BEFORE UPDATE OF id, ts, run_id, gate_name, verdict, concern, evidence_trace_ids, evidence_summary, suggestion\" \"  ON grounded_rejections\" \"  BEGIN\" \"    SELECT RAISE(ABORT, '\"'\"'grounded_rejections: provenance fields are immutable; only consumed_by_reflector_ts may be set'\"'\"');\" \"  END;\" \"\" \"CREATE TRIGGER IF NOT EXISTS grounded_rejections_no_delete\" \"  BEFORE DELETE ON grounded_rejections\" \"  BEGIN\" \"    SELECT RAISE(ABORT, '\"'\"'grounded_rejections is append-only'\"'\"');\" \"  END;\"'"

PRAGMA foreign_keys = ON;
