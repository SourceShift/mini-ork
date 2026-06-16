-- 0036_safety_events.sql
-- Tier 2 Item 2: safety_events table for tripwire/incident logging.
--
-- Backs the commitments in docs/RSP.md § 3 (tripwires) and § 4.1
-- (post-incident reports within 30 days). Append-only by trigger,
-- mirroring the audit_log convention at docs/SAFETY.md:116-127.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS safety_events (
  id                TEXT PRIMARY KEY,
  ts                INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  tripwire_id       TEXT NOT NULL,
  run_id            TEXT,
  recipe            TEXT,
  severity          TEXT NOT NULL CHECK (severity IN ('critical','high','medium','low')),
  status            TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open','acknowledged','resolved','dismissed')),
  evidence_json     TEXT NOT NULL,
  operator_response TEXT,
  resolution_ts     INTEGER,
  resolution_note   TEXT
);

CREATE INDEX IF NOT EXISTS idx_safety_events_ts
  ON safety_events(ts DESC);

CREATE INDEX IF NOT EXISTS idx_safety_events_status_open
  ON safety_events(status) WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_safety_events_tripwire
  ON safety_events(tripwire_id, ts DESC);

CREATE TRIGGER IF NOT EXISTS safety_events_no_immutable_update
  BEFORE UPDATE OF id, ts, tripwire_id, run_id, recipe, severity, evidence_json
  ON safety_events
  BEGIN
    SELECT RAISE(ABORT, 'safety_events: id/ts/tripwire_id/run_id/recipe/severity/evidence_json are immutable');
  END;

CREATE TRIGGER IF NOT EXISTS safety_events_no_delete
  BEFORE DELETE ON safety_events
  BEGIN
    SELECT RAISE(ABORT, 'safety_events is append-only; use the operator-run aging job');
  END;

PRAGMA foreign_keys = ON;
