-- 0026_policy_state.sql — stateful policy substrate.
--
-- Implements Epic E2 of the Omnigent-improvement plan
-- (.mini-ork/kickoffs/omnigent-phase-e2-policy-engine.md) per the
-- panel-revised ordering at
-- docs/research/omnigent-vs-mini-ork-panel-synthesis.md.
--
-- Two tables:
--   policy_state      KV per (run_id, key); read + mutated by
--                     stateful policy callables.
--   policy_decisions  audit row per policy evaluation.
--
-- Why two tables instead of one: state is dynamic + writeable;
-- decisions are append-only + read-mostly. Mixing them confuses
-- the access pattern and complicates retention policy (you may
-- want to expire old state but keep all decisions forever).

CREATE TABLE IF NOT EXISTS policy_state (
    run_id      TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT,
    updated_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    PRIMARY KEY (run_id, key)
);

CREATE INDEX IF NOT EXISTS idx_policy_state_updated
    ON policy_state(updated_at);


CREATE TABLE IF NOT EXISTS policy_decisions (
    decision_id    TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    policy_name    TEXT NOT NULL,
    result         TEXT NOT NULL
                       CHECK(result IN ('ALLOW', 'DENY',
                                        'REQUIRE_APPROVAL', 'LOG_ONLY')),
    reason         TEXT,
    evaluated_at   INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    payload_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_policy_decisions_run
    ON policy_decisions(run_id, evaluated_at);

CREATE INDEX IF NOT EXISTS idx_policy_decisions_policy
    ON policy_decisions(policy_name, evaluated_at);
