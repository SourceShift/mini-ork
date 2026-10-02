-- 0061_remote_procs.sql
-- remote-nodes epic 10: disconnect-tolerance, reattach and resume
-- (kickoff remote-nodes-10-disconnect-reattach-resume.md).
--
-- A control-plane mirror of the node-agent's per-session proc registry
-- (mini_ork/remote/node_agent/procs.py::ProcRegistry). The node-agent
-- already persists out_offset / err_offset per pid so a re-connecting
-- client can re-stream from a byte offset (ProcRegistry._persist:245-266,
-- _watch_proc:370-371). The control plane needs the SAME state so a
-- ``mini-ork recover --strategy reattach`` can decide "is the proc still
-- running on the VM, can I re-attach, or must I retry from scratch?"
-- without an extra round-trip to the node-agent.
--
-- ADditive only. No existing column is altered / dropped / backfilled;
-- legacy runs (epic 06/07/08/09) are unaffected because nothing writes
-- to remote_procs until the new client code lands.
--
-- Concurrency: single-writer model assumed (one control plane owns the
-- run; one node-agent owns the session). On a re-dispatch the client
-- UPSERTs the row keyed by ``idempotency_key`` (a sha256 of run_id,
-- node_id, attempt, input_hash — mirrors the durable-DAG input hash on
-- node_checkpoints). The PK is (run_id, node_id, attempt) so a retry
-- produces a NEW row; idempotency_key UNIQUE catches double-writes from
-- a concurrent re-dispatch path.
--
-- Offset columns mirror ProcRegistry._persist and are checkpointed at
-- least every 5 s while streaming (epic 10 §3 client-side reconnect loop).

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS remote_procs (
    run_id          TEXT    NOT NULL,
    node_id         TEXT    NOT NULL,
    attempt         INTEGER NOT NULL,
    node_host       TEXT    NOT NULL,         -- mini_ork.remote.nodes.Node.name
    session_id      TEXT,                     -- the session's sid (or run_id fallback)
    proc_id         INTEGER NOT NULL,         -- node-agent's per-session pid sequence
    idempotency_key TEXT    NOT NULL,         -- sha256(run_id|node_id|attempt|input_hash)
    state           TEXT    NOT NULL,         -- starting|running|exited|killed|orphaned|spawn_failed|timeout|detached
    rc              INTEGER,
    out_offset      INTEGER NOT NULL DEFAULT 0,
    err_offset      INTEGER NOT NULL DEFAULT 0,
    started_at      INTEGER NOT NULL,         -- unix epoch seconds
    ended_at        INTEGER,                  -- NULL while in flight
    PRIMARY KEY (run_id, node_id, attempt)
);

-- The dedup key (kickoff §1: same attempt re-dispatched → same row).
CREATE UNIQUE INDEX IF NOT EXISTS idx_remote_procs_idempotency
  ON remote_procs(idempotency_key);

-- Hot read: "is there an unconsumed row for the first-incomplete node
-- of run X?" — used by plan_recovery(strategy="reattach") to probe
-- whether to set strategy=reattach as the default for a recovery.
CREATE INDEX IF NOT EXISTS idx_remote_procs_run_node
  ON remote_procs(run_id, node_id);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0061_remote_procs.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'remote-procs-v1');
