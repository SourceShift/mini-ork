-- 0052_run_leases_recovery_requests.sql
-- E3 of kickoff feat/durable-dag: single-writer lease + fencing
-- (numbered 0052 to clear a collision with main's 0051_conductor_epic_optional
--  on the shared state.db; runner keys on filename so this is additive.)
-- (run_leases) + idempotent recovery requests (recovery_requests).
--
-- Source of truth: internal-docs/architecture/2026-07-15-durable-dag-resume-design.md
-- §2 (durable state model — run_leases, recovery_requests),
-- §7 (single-writer ownership — fencing tokens).
--
-- Concurrency model (SQLite WAL): fencing reduces to a single atomic
-- UPDATE...WHERE owner_token=:token that returns rowcount (rejected on
-- rowcount=0). The lease module in mini_ork/ported/lease.py is the
-- only writer of run_leases; concurrent recovery requests race on
-- INSERT OR IGNORE into recovery_requests (the row's existence makes
-- the second call idempotent). No other writer may touch these tables.
--
-- Compatibility: additive only, no ALTERs on existing schema. Legacy
-- runs without a run_leases row are treated as "no active lease" — the
-- lease module treats absence as acquire-eligible (the very first
-- recovery for a run creates the row). E2 (recovery_planner) reads
-- run_leases only when present.

PRAGMA foreign_keys = OFF;

-- run_leases: one row per run_id (PK). Single-writer fencing token.
-- A recovery acquires the lease (mint owner_token, set expires_at)
-- before any dispatch or checkpoint write. A stale worker presenting
-- the wrong owner_token — including a previously-valid but expired
-- token — has its UPDATE rejected (rowcount == 0 → fence failure).
--
-- expires_at is a wall-clock epoch in seconds. Default lease TTL is
-- bounded by lease_ttl_s at acquire-time; the runtime calls refresh()
-- to extend while still alive. expired leases can be re-acquired by
-- any caller (no live holder → the row's owner_token is stale).
CREATE TABLE IF NOT EXISTS run_leases (
    run_id        TEXT    PRIMARY KEY,
    owner_token   TEXT    NOT NULL,
    acquired_at   INTEGER NOT NULL,   -- unix epoch seconds
    expires_at    INTEGER NOT NULL,   -- unix epoch seconds; <= now → expired
    renewed_at    INTEGER NOT NULL,   -- unix epoch seconds; == acquired_at if never refreshed
    check (expires_at >= acquired_at)
);

-- recovery_requests: idempotency + audit. Same (run_id, from_node,
-- strategy) returns the existing row instead of triggering a fresh
-- dispatch. status moves pending → dispatched → completed|failed;
-- request_id is the row's PK and is the public handle the dispatch
-- loop stamps on its work. budget_usd is the per-recovery cost ceiling
-- (the design's "auto-retry must be budget-bounded" requirement).
CREATE TABLE IF NOT EXISTS recovery_requests (
    request_id           TEXT    PRIMARY KEY,
    run_id               TEXT    NOT NULL,
    from_node            TEXT    NOT NULL,
    strategy             TEXT    NOT NULL,
    status               TEXT    NOT NULL CHECK (status IN ('pending','dispatched','completed','failed')),
    failure_class        TEXT,                -- populated when status moves to failed/completed
    budget_usd           REAL    NOT NULL DEFAULT 5.00,
    cost_usd             REAL    NOT NULL DEFAULT 0.0,
    dispatch_count       INTEGER NOT NULL DEFAULT 0,
    owner_token          TEXT,                -- run_leases.owner_token at dispatch time (fence audit)
    created_at           INTEGER NOT NULL,
    last_dispatched_at   INTEGER,
    closed_at            INTEGER,
    payload_json         TEXT                 -- opaque per-strategy payload (strategy hints, etc.)
);

-- Idempotency key — a duplicate INSERT collides on this index.
-- The lease module relies on the row's existence (not just the PK) to
-- answer "is a recovery in flight?"; the unique index keeps concurrent
-- INSERTs from creating a second row.
CREATE UNIQUE INDEX IF NOT EXISTS uq_recovery_requests_idem
    ON recovery_requests(run_id, from_node, strategy);

-- Hot read for the recovery dispatch loop: "show me the in-flight or
-- last-completed recovery for this (run, from_node, strategy)" —
-- planner queries this to short-circuit a duplicate dispatch.
CREATE INDEX IF NOT EXISTS idx_recovery_requests_status
    ON recovery_requests(run_id, status);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0052_run_leases_recovery_requests.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'durable-dag-e3-v1');