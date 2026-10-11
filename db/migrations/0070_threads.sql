-- 0070_threads.sql
-- Threads: a durable, multi-turn unit of work, so a conversation can be
-- followed up instead of ending with "start a new conversation".
--
-- Until now the only conversation model was the OpenHands agent-server shim:
-- one JSON sidecar per conversation, one DAG inside it, and a 409 on any
-- second message (web/routes/agent_server.py). A thread is what makes "same
-- thread, next turn" expressible — and it is the surface every later feature
-- attaches to: work-intake opens a thread from a ticket, a chat transport
-- opens one from a message, an attachment hangs off a thread.
--
--   threads         -- one row per conversation. id == conversation_id, which
--                      is also the attempt-1 run id (create_conversation passes
--                      the client uuid straight to launch_run), so the SPA, the
--                      websocket, MINI_ORK_RUN_ID and this table agree on one
--                      key with no translation table.
--   thread_messages -- the transcript, including turns a remote surface
--                      contributed. `event_id` makes a run_events projection
--                      exactly-once; `external_id` makes a Slack/Telegram
--                      redelivery a no-op instead of a second turn.
--   thread_runs     -- one row per launch. attempt 1 is the initial run;
--                      attempt N (N>1) is a continuation in the SAME thread,
--                      which is what replaces the 409.
--   intake_events   -- the ledger of what a work-intake trigger has already
--                      acted on: webhook replay protection, and a re-poll
--                      folding instead of re-dispatching.
--
-- All four are ADDITIVE. The reader is mini_ork/threads/store.py, which also
-- runs this same DDL at runtime (ensure_schema) so a state.db that never ran a
-- migration still works — the precedent 0069_pm_mirror.sql and
-- scheduler.ensure_retry_schema set. tests/unit/test_threads_store.py asserts
-- the two texts agree, so they cannot drift apart quietly.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS threads (
  id          TEXT PRIMARY KEY,        -- == conversation_id; attempt-1 run id
  title       TEXT NOT NULL DEFAULT '',
  -- No CHECK on the evolving vocabularies (status, origin, role): a newer
  -- mini-ork may write a value this reader does not know, and it must degrade
  -- to a safe default rather than refuse the row — the rule pm_issues.state
  -- set. The store's readers tolerate the unknown (thread_state → idle).
  status      TEXT NOT NULL DEFAULT 'idle',
  origin      TEXT NOT NULL DEFAULT 'ide',  -- closed set: ide|chat|intake|cli
  origin_ref  TEXT,                    -- provider key: 'github:o/r#12', 'slack:C/T'
  recipe      TEXT,                    -- recipe each new attempt launches with
  last_turn   INTEGER NOT NULL DEFAULT 0,  -- turn allocator, taken under BEGIN IMMEDIATE
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- Hot read: "which thread owns this ticket?" (intake dedup).
CREATE INDEX IF NOT EXISTS idx_threads_origin_ref ON threads(origin, origin_ref);

-- Hot read: the sidebar, newest first.
CREATE INDEX IF NOT EXISTS idx_threads_updated ON threads(updated_at);

CREATE TABLE IF NOT EXISTS thread_messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- stable intra-turn order
  thread_id   TEXT NOT NULL,
  turn        INTEGER NOT NULL,      -- the round this belongs to (== attempt)
  role        TEXT NOT NULL,
  text        TEXT NOT NULL DEFAULT '',
  event_id    TEXT,                  -- run_events.event_id when projected
  external_id TEXT,                  -- slack ts / telegram update_id / …
  attachments TEXT,                  -- JSON array of stored paths
  created_at  TEXT NOT NULL
);

-- Hot read: the transcript, in order.
CREATE INDEX IF NOT EXISTS idx_thread_messages_thread ON thread_messages(thread_id, turn, id);

-- Exactly-once projection of a run_events row. observe() runs on every poll
-- tick, so without this a re-projection appends duplicates forever. Partial,
-- because most rows (a user's own turn) carry no event id and many NULLs are
-- not a uniqueness claim.
CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_messages_event
  ON thread_messages(thread_id, event_id) WHERE event_id IS NOT NULL;

-- A redelivered remote message is the same message, not a second turn.
CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_messages_external
  ON thread_messages(thread_id, external_id) WHERE external_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS thread_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id     TEXT NOT NULL,
  run_id        TEXT NOT NULL,       -- == thread_id for attempt 1; '{id}-t{turn}' after
  attempt       INTEGER NOT NULL,    -- 1, 2, 3 … one per launch
  status        TEXT NOT NULL,
  recipe        TEXT,
  kickoff_path  TEXT,
  started_at    TEXT NOT NULL,
  ended_at      TEXT
);

-- One attempt number never repeats: the DB-level guard against a poller and a
-- webhook racing to dispatch attempt N twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_runs_attempt ON thread_runs(thread_id, attempt);
CREATE INDEX IF NOT EXISTS idx_thread_runs_run ON thread_runs(run_id);

-- Hot read: "is a run live for this thread?". Partial, so the index holds only
-- the handful of in-flight runs rather than every run ever launched.
CREATE INDEX IF NOT EXISTS idx_thread_runs_live
  ON thread_runs(thread_id) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS intake_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  provider     TEXT NOT NULL,
  delivery_id  TEXT NOT NULL,        -- webhook delivery / poll fingerprint
  payload_hash TEXT NOT NULL,        -- same event, same body?
  issue_uid    TEXT,
  thread_id    TEXT,
  action       TEXT,                 -- launched|steered|continued|duplicate|skipped|error
  detail       TEXT,
  created_at   TEXT NOT NULL,
  UNIQUE (provider, delivery_id)
);

CREATE INDEX IF NOT EXISTS idx_intake_events_thread ON intake_events(thread_id);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0070_threads.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'threads-v1');
