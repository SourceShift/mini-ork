-- 0030_operator_steering.sql
-- Operator-injected steering messages that surface in the next node's
-- context_assemble pack. Provides a bidirectional supervisor channel:
-- external observers (chat bots, dashboards, human operators) can inject
-- guidance into an in-flight run between nodes, and mini-ork's planner /
-- implementer / reviewer prompts see it on their next dispatch.
--
-- Schema design:
--   - run_id          : target a specific in-flight run (NULL = next planner
--                       run of any kind — global queue)
--   - role_target     : the agent role the message is addressed to
--                       ("planner" / "implementer" / "reviewer" / "any")
--   - severity        : "info" / "warn" / "critical" — context_assembler
--                       ranks higher severities up the context window
--   - message         : the steering text the agent will see
--   - source          : free-form provenance string (e.g. "claude-code",
--                       "operator-cli", "dashboard:bgxkii4eu")
--   - confidence      : 0.0-1.0 — context_assembler can rank by this
--   - created_at      : unix ms timestamp
--   - consumed_at     : unix ms timestamp; NULL until a context_assemble
--                       call reads this row. Old rows are NOT auto-deleted
--                       so they remain available as historical audit trail.
--   - expires_at      : unix ms timestamp; rows past their TTL are skipped
--                       by context_assembler. Default 1 hour.
--
-- Indexes optimized for the two hot reads:
--   - by run_id + unconsumed + unexpired (per-run context_assemble path)
--   - by NULL run_id + unconsumed (global queue for next planner dispatch)

CREATE TABLE IF NOT EXISTS operator_steering (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    role_target     TEXT NOT NULL CHECK (role_target IN ('planner','implementer','reviewer','verifier','any')),
    severity        TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info','warn','critical')),
    message         TEXT NOT NULL,
    source          TEXT,
    confidence      REAL NOT NULL DEFAULT 0.8 CHECK (confidence BETWEEN 0 AND 1),
    created_at      INTEGER NOT NULL,
    consumed_at     INTEGER,
    expires_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operator_steering_run_unconsumed
    ON operator_steering(run_id, consumed_at, expires_at)
    WHERE consumed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_operator_steering_global_unconsumed
    ON operator_steering(consumed_at, expires_at)
    WHERE run_id IS NULL AND consumed_at IS NULL;
