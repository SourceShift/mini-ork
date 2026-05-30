-- mini-ork migration 0002 — mini-orch session cache + dispatch tracking
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0002_mini_orch_sessions.sql
BEGIN;

-- ── orch_dispatches ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orch_dispatches (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_dispatch_id  INTEGER REFERENCES orch_dispatches(id),
  epic_id             TEXT NOT NULL,               -- no FK: mini-orch may dispatch before epics row exists
  group_id            TEXT,                        -- e.g. "bcf", "user-menu-v8"
  dispatched_by       TEXT NOT NULL CHECK (dispatched_by IN
    ('claude-session','orchestrator','human-cli','scaffold')),
  claude_session_id   TEXT,
  zellij_session_name TEXT,
  kickoff_path        TEXT,
  run_dir             TEXT,
  status              TEXT NOT NULL CHECK (status IN
    ('pending','in_progress','fanned_out','completed','cancelled')),
  rationale           TEXT,
  created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  closed_at           TEXT,
  test_status         TEXT
    CHECK (test_status IN ('pass','fail','skip','pending') OR test_status IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_orch_dispatches_epic    ON orch_dispatches(epic_id);
CREATE INDEX IF NOT EXISTS idx_orch_dispatches_status  ON orch_dispatches(status);
CREATE INDEX IF NOT EXISTS idx_orch_dispatches_session ON orch_dispatches(claude_session_id);
CREATE INDEX IF NOT EXISTS idx_orch_dispatches_active
  ON orch_dispatches(epic_id) WHERE status NOT IN ('completed','cancelled');
CREATE INDEX IF NOT EXISTS idx_orch_dispatches_test_status
  ON orch_dispatches(test_status) WHERE test_status IS NOT NULL;

-- ── mini_orch_sessions ────────────────────────────────────────────────────────
-- Cache table: stores input/output of each stage so identical inputs can be
-- replayed without paying LLM cost again (reused_count tracks savings).
CREATE TABLE IF NOT EXISTS mini_orch_sessions (
  uuid           TEXT PRIMARY KEY,
  job_id         TEXT NOT NULL,
  epic_id        TEXT NOT NULL,
  iter           INTEGER NOT NULL,
  stage          TEXT NOT NULL CHECK (stage IN (
                   'spec-author','spec-reviewer','mutation-adversary',
                   'mutation-validator','rubric','worker','reviewer',
                   'bdd-runner','reflection-refiner'
                 )),
  input_hash     TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('running','success','failed','resumable')),
  output_path    TEXT,
  log_path       TEXT,
  cost_usd       NUMERIC,
  turns          INTEGER,
  duration_ms    INTEGER,
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  expires_at     TEXT NOT NULL,
  reused_count   INTEGER NOT NULL DEFAULT 0,
  prompt_version TEXT
);

CREATE INDEX IF NOT EXISTS mos_lookup ON mini_orch_sessions (
  epic_id, iter, stage, input_hash, status, expires_at
);
CREATE INDEX IF NOT EXISTS mos_gc ON mini_orch_sessions (expires_at);

CREATE VIEW IF NOT EXISTS mini_orch_cache_stats AS
SELECT
  stage,
  COUNT(*) AS rows_total,
  SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS rows_success,
  SUM(reused_count) AS times_reused,
  ROUND(SUM(CASE WHEN reused_count > 0 THEN cost_usd * reused_count ELSE 0 END), 2) AS dollars_saved,
  ROUND(SUM(cost_usd), 2) AS dollars_spent
FROM mini_orch_sessions
GROUP BY stage;

-- ── mo_events ─────────────────────────────────────────────────────────────────
-- Append-only event log for mini-orch lifecycle — every spawn, verdict, merge,
-- LLM call, and cost row is recorded here for OTel export and dashboard replay.
CREATE TABLE IF NOT EXISTS mo_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  epic_id         TEXT NOT NULL,
  dispatch_id     INTEGER REFERENCES orch_dispatches(id),
  run_id          INTEGER REFERENCES runs(id),
  iter            INTEGER,
  ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  duration_ms     INTEGER,
  event_type      TEXT NOT NULL CHECK (event_type IN (
    -- lifecycle
    'epic_picked','dispatch_open','dispatch_close',
    -- worker
    'worker_spawn','worker_exit','worker_iter_start','worker_iter_end',
    -- review
    'reviewer_spawn','reviewer_verdict',
    -- bdd / trace / debugger
    'bdd_run','bdd_verdict','trace_verdict','debugger_spawn','debugger_verdict',
    -- conflict tiers
    'conflict_detected','conflict_tier1','conflict_tier2','conflict_tier3','conflict_tier4',
    -- merge
    'merge_attempt','merge_success','merge_abort',
    -- decisions
    'brain_decision','agent_reassign','escalation','salvage_dispatch',
    -- llm / cost
    'llm_call','cost_row',
    -- inbox / scope
    'inbox_write','scope_violation','scope_revert',
    -- generic
    'note'
  )),
  actor           TEXT,
  status          TEXT CHECK (status IN ('start','ok','fail','skip','pending') OR status IS NULL),
  artifact_path   TEXT,
  parent_event_id INTEGER REFERENCES mo_events(id),
  cost_usd        REAL,
  trace_id        TEXT,                            -- W3C trace_id for OTel correlation
  payload_json    TEXT                             -- typed JSON blob per event_type
);

CREATE INDEX IF NOT EXISTS idx_mo_events_epic_ts    ON mo_events(epic_id, ts);
CREATE INDEX IF NOT EXISTS idx_mo_events_dispatch   ON mo_events(dispatch_id) WHERE dispatch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mo_events_run        ON mo_events(run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mo_events_event_type ON mo_events(event_type);
CREATE INDEX IF NOT EXISTS idx_mo_events_trace      ON mo_events(trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mo_events_parent     ON mo_events(parent_event_id) WHERE parent_event_id IS NOT NULL;

-- ── mo_events_archive ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mo_events_archive (
  id              INTEGER PRIMARY KEY,
  epic_id         TEXT NOT NULL,
  dispatch_id     INTEGER,
  run_id          INTEGER,
  iter            INTEGER,
  ts              TEXT NOT NULL,
  duration_ms     INTEGER,
  event_type      TEXT NOT NULL,
  actor           TEXT,
  status          TEXT,
  artifact_path   TEXT,
  parent_event_id INTEGER,
  cost_usd        REAL,
  trace_id        TEXT,
  payload_json    TEXT,
  archived_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_mo_events_archive_epic_ts ON mo_events_archive(epic_id, ts);

-- ── agent_messages ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  from_session    TEXT NOT NULL,
  from_role       TEXT,
  to_session      TEXT,
  to_role         TEXT,
  topic           TEXT,
  kind            TEXT NOT NULL CHECK (kind IN ('ask','tell','reply','heartbeat','subscribe')),
  body_json       TEXT NOT NULL DEFAULT '{}',
  reply_to_id     INTEGER REFERENCES agent_messages(id),
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','delivered','answered','expired','failed','rejected')),
  ts_created      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ts_delivered    TEXT,
  ts_answered     TEXT,
  ttl_seconds     INTEGER NOT NULL DEFAULT 300,
  depth           INTEGER NOT NULL DEFAULT 0,      -- prevents A→B→A loops; mediator rejects depth≥3
  cost_usd        REAL,
  trace_id        TEXT,
  epic_id         TEXT,
  error_msg       TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_msgs_to_pending
  ON agent_messages(to_session, status) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_agent_msgs_to_role_pending
  ON agent_messages(to_role, status) WHERE to_role IS NOT NULL AND status='pending';
CREATE INDEX IF NOT EXISTS idx_agent_msgs_topic
  ON agent_messages(topic) WHERE topic IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_msgs_reply_to
  ON agent_messages(reply_to_id) WHERE reply_to_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_msgs_epic
  ON agent_messages(epic_id) WHERE epic_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_agent_msgs_expire_check
AFTER UPDATE OF status ON agent_messages
FOR EACH ROW
WHEN NEW.status = 'pending'
  AND (julianday('now') - julianday(NEW.ts_created)) * 86400 > NEW.ttl_seconds
BEGIN
  UPDATE agent_messages SET status='expired',
                            error_msg='ttl_exceeded'
   WHERE id = NEW.id;
END;

CREATE TABLE IF NOT EXISTS agent_session_locks (
  session_id      TEXT PRIMARY KEY,
  acquired_by     TEXT NOT NULL,
  acquired_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  expires_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_session_locks_expires
  ON agent_session_locks(expires_at);

CREATE TABLE IF NOT EXISTS agent_scope_claims (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL,
  role        TEXT,
  epic_id     TEXT,
  pattern     TEXT NOT NULL,
  claimed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  expires_at  TEXT NOT NULL,
  released_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_scope_claims_active
  ON agent_scope_claims(pattern) WHERE released_at IS NULL;

-- ── llm_calls ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_calls (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  provider        TEXT NOT NULL,                   -- 'anthropic' | 'google' | 'openai' | 'deepseek' | 'openrouter'
  model_id        TEXT NOT NULL,
  tier            TEXT NOT NULL,                   -- 'fast' | 'default' | 'smart' | 'pro' | 'reasoning' | 'embedding'
  feature_name    TEXT NOT NULL,                   -- 'mini-orch:detective' | 'mini-orch:reviewer' | ...
  actor           TEXT,
  epic_id         TEXT,
  dispatch_id     INTEGER,
  run_id          INTEGER,
  iter            INTEGER,
  input_tokens    INTEGER NOT NULL DEFAULT 0,
  output_tokens   INTEGER NOT NULL DEFAULT 0,
  total_tokens    INTEGER NOT NULL DEFAULT 0,
  cost_usd        REAL NOT NULL DEFAULT 0,
  duration_ms     INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL CHECK (status IN ('success','failed')),
  finish_reason   TEXT,
  error_message   TEXT,
  traceparent     TEXT,                            -- W3C 'version-traceid-spanid-flags'
  metadata_json   TEXT NOT NULL DEFAULT '{}',
  ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_epic
  ON llm_calls(epic_id) WHERE epic_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_calls_run
  ON llm_calls(run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_calls_feature
  ON llm_calls(feature_name);
CREATE INDEX IF NOT EXISTS idx_llm_calls_actor
  ON llm_calls(actor) WHERE actor IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_calls_ts
  ON llm_calls(ts);
CREATE INDEX IF NOT EXISTS idx_llm_calls_provider_model
  ON llm_calls(provider, model_id);

-- ── mo_inbox_gates ────────────────────────────────────────────────────────────
-- Human-approval gates inserted into the mini-orch flow at key phase boundaries.
CREATE TABLE IF NOT EXISTS mo_inbox_gates (
  inbox_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  gate_id         TEXT NOT NULL,
  feature         TEXT NOT NULL,
  phase           TEXT,
  context_json    TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  review_note     TEXT,
  enqueued_at     INTEGER NOT NULL,
  resolved_at     INTEGER,
  blocks_dispatch_for TEXT
);

CREATE INDEX IF NOT EXISTS idx_mo_inbox_gates_pending
  ON mo_inbox_gates(status) WHERE status='pending';

COMMIT;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0002_mini_orch_sessions.sql', strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'v1');
