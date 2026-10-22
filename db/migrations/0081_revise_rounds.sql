-- 0081_revise_rounds.sql — CoBa: per-round revise history.
-- Kickoff: kickoffs/auto/rsi-gap-coba.md.
--
-- The revise loop spends up to ``MO_REVISE_ROUNDS`` implementer rounds whatever
-- the history says a further round is worth. This table is that history: one
-- row per granted revise round, settled to 1 (passed) / 0 (failed) when the
-- source that fired it reports again. ``mini_ork/learning/revise_value.py``
-- reads it to estimate the next round's success probability and stop early.
--
--   id          INTEGER PRIMARY KEY
--   run_id      TEXT NOT NULL       the run that granted the round
--   task_class  TEXT                scopes the history pools
--   target_node TEXT NOT NULL       the revise target (usually the implementer)
--   round       INTEGER NOT NULL    the granted round number (1-based)
--   passed      INTEGER             NULL until known, 1 passed, 0 failed
--   cost_usd    REAL                what the round cost, when known
--   created_at  INTEGER NOT NULL    epoch seconds
--
-- Unique ``(run_id, target_node, round)`` makes ``record_round`` idempotent.
-- ADDITIVE only. ``revise_value.record_round`` runs the same DDL at runtime so
-- a state.db that never runs this migration still works — the two texts must
-- not drift apart.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS revise_rounds (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT    NOT NULL,
    task_class  TEXT,
    target_node TEXT    NOT NULL,
    round       INTEGER NOT NULL,
    passed      INTEGER,
    cost_usd    REAL,
    created_at  INTEGER NOT NULL,
    UNIQUE (run_id, target_node, round)
);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0081_revise_rounds.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'revise-rounds-v1');
