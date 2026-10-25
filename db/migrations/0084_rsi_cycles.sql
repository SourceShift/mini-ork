-- 0084_rsi_cycles.sql — ledger for the self-directed RSI cycle.
--
-- The cycle (docs/plans/2026-10-23-rsi-cycle.md) harvests gaps in learning,
-- routing and verification, ranks them into one queue, dispatches a
-- framework-edit child to fix the top item, validates the result with gates it
-- cannot influence, and merges or quarantines. Without a ledger the next cycle
-- re-derives the same twenty gaps and re-attempts the ones that already failed.
-- These four tables are that memory; `rsi_candidates.signature` is the key that
-- turns "is this already fixed?" and "did we already fail at this?" into one
-- indexed lookup instead of a re-run.
--
-- TIMESTAMPS ARE INTEGER EPOCH, deliberately. The live DB carries 43/47
-- `promotion_records.decided_at` and 43/43 `apply_attempts.created_at` values
-- written with a malformed `:fZ` literal, which sorts lexicographically instead
-- of temporally — that corruption is itself candidate L4. Integer columns make
-- `ORDER BY started_at DESC` and "newer than N days" honest by construction.
-- The trailing `schema_migrations` row keeps the project-wide
-- `strftime('%Y-%m-%dT%H:%M:%fZ','now')` convention; nothing reads it
-- temporally.
--
-- ADDITIVE only: four new tables, no ALTER, so a plain `CREATE TABLE IF NOT
-- EXISTS` is enough — re-running the file is a no-op.
--
-- Note on `rsi_cycles.status`: `running` is written at start and must be
-- overwritten on every exit path. A row left `running` past its lease TTL is a
-- crashed cycle, and `rsi-cycle doctor` reports it; it is never auto-resolved,
-- because guessing the outcome is how a ledger starts lying.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS rsi_cycles (
  cycle_id          TEXT PRIMARY KEY,
  started_at        INTEGER NOT NULL,
  finished_at       INTEGER,
  mode              TEXT NOT NULL,
  status            TEXT NOT NULL,
  halt_reason       TEXT,
  lease_token       TEXT,
  dry_run           INTEGER NOT NULL DEFAULT 0,
  harvest_ok        INTEGER,
  probes_run        INTEGER NOT NULL DEFAULT 0,
  probes_error      INTEGER NOT NULL DEFAULT 0,
  candidates_found  INTEGER NOT NULL DEFAULT 0,
  candidates_queued INTEGER NOT NULL DEFAULT 0,
  domain            TEXT,
  candidate_id      TEXT,
  signature         TEXT,
  source            TEXT,
  lens              TEXT,
  suggest_agreement REAL,
  child_run_id      TEXT,
  gates_json        TEXT,
  critic_json       TEXT,
  metric_before     TEXT,
  metric_after      TEXT,
  baseline_json     TEXT,
  cost_usd          REAL NOT NULL DEFAULT 0,
  merged_sha        TEXT,
  quarantine_reason TEXT,
  quarantine_path   TEXT
);

CREATE INDEX IF NOT EXISTS ix_rsi_cycles_started ON rsi_cycles(started_at);

CREATE TABLE IF NOT EXISTS rsi_candidates (
  candidate_id    TEXT PRIMARY KEY,
  signature       TEXT NOT NULL,
  domain          TEXT NOT NULL,
  source          TEXT NOT NULL,
  kind            TEXT NOT NULL,
  severity        INTEGER NOT NULL,
  blast_radius    INTEGER NOT NULL DEFAULT 1,
  files_json      TEXT NOT NULL DEFAULT '[]',
  evidence_key    TEXT NOT NULL,
  evidence_json   TEXT NOT NULL DEFAULT '{}',
  title           TEXT,
  success_metric  TEXT,
  success_command TEXT,
  first_seen_at   INTEGER NOT NULL,
  last_seen_at    INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL,
  times_seen      INTEGER NOT NULL DEFAULT 1,
  status          TEXT NOT NULL DEFAULT 'open',
  outcome_reason  TEXT,
  merged_sha      TEXT
);

-- The avoidance query: one index covers both "already merged" and "already
-- failed recently" — status first, then the signature, then recency.
CREATE INDEX IF NOT EXISTS ix_rsi_candidates_sig
  ON rsi_candidates(signature, status, updated_at);

CREATE INDEX IF NOT EXISTS ix_rsi_candidates_rank
  ON rsi_candidates(status, severity DESC);

CREATE TABLE IF NOT EXISTS rsi_probe_runs (
  id             INTEGER PRIMARY KEY,
  cycle_id       TEXT NOT NULL,
  probe_id       TEXT NOT NULL,
  cmd            TEXT NOT NULL,
  status         TEXT NOT NULL,
  rc             INTEGER,
  duration_ms    INTEGER,
  findings_count INTEGER NOT NULL DEFAULT 0,
  payload_json   TEXT,
  stderr_digest  TEXT,
  created_at     INTEGER NOT NULL
);

-- One row per probe per cycle: a re-run of the same probe inside one cycle
-- replaces its row instead of inflating counts.
CREATE UNIQUE INDEX IF NOT EXISTS ux_rsi_probe_runs
  ON rsi_probe_runs(cycle_id, probe_id);

CREATE TABLE IF NOT EXISTS rsi_credit (
  id           INTEGER PRIMARY KEY,
  cycle_id     TEXT NOT NULL,
  candidate_id TEXT,
  source       TEXT NOT NULL,
  lens         TEXT,
  domain       TEXT NOT NULL,
  outcome      TEXT NOT NULL,
  severity     INTEGER NOT NULL DEFAULT 0,
  utility      REAL NOT NULL DEFAULT 0,
  cost_usd     REAL NOT NULL DEFAULT 0,
  created_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_rsi_credit_source ON rsi_credit(source, lens);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0084_rsi_cycles.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'rsi-cycles-v1');
