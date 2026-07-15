-- 0053_tool_receipts.sql
-- E4 of kickoff feat/durable-dag: tool-call receipts for safe replay.
-- (numbered 0053: 0050=node_checkpoints, 0052=run_leases; 0051 is main's
--  conductor migration on the shared state.db — runner keys on filename.)
--
-- Source of truth: internal-docs/architecture/2026-07-15-durable-dag-resume-design.md §6.
--
-- A recovered node may replay work it already did. A side-effecting
-- (non-idempotent) tool call — a commit, a file write, an external POST —
-- must NOT run twice. Before a node is considered done, each such call
-- persists a receipt (input hash + captured output). On replay a completed
-- non-idempotent tool returns its receipt and is never re-invoked
-- (scenario 8). Read-only tools carry idempotent=1 and may replay per
-- strategy.
--
-- Compatibility: additive only. The unique index makes a re-recorded
-- (run,node,tool,input_hash) an UPSERT, not a duplicate.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS tool_receipts (
    receipt_id     TEXT    PRIMARY KEY,
    run_id         TEXT    NOT NULL,
    node_id        TEXT    NOT NULL,
    attempt        INTEGER NOT NULL DEFAULT 1,
    tool_name      TEXT    NOT NULL,
    input_hash     TEXT    NOT NULL,          -- sha256 of the canonical tool input
    idempotent     INTEGER NOT NULL DEFAULT 0, -- 1 = read-only, safe to replay
    output_json    TEXT,                       -- the receipt payload (captured output)
    status         TEXT    NOT NULL DEFAULT 'completed'
                   CHECK (status IN ('completed','failed')),
    created_at     INTEGER NOT NULL
);

-- Idempotency key: one receipt per (run, node, tool, input). A repeat
-- record for the same call UPSERTs rather than duplicating.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tool_receipts
    ON tool_receipts(run_id, node_id, tool_name, input_hash);

-- Hot read for the replay guard: "do I already have a receipt for this
-- node's tool calls?"
CREATE INDEX IF NOT EXISTS idx_tool_receipts_node
    ON tool_receipts(run_id, node_id);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0053_tool_receipts.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'durable-dag-e4-v1');
