-- 0082_context_item_utility.sql — close the injection-outcome loop for the v2 pack.
--
-- The per-run injection ledger (``runs/<id>/learned/<node>.json``) already names
-- every pack item a node was given (``sources[].id`` such as ``t:<theme_id>`` and
-- ``f:<hash>``) and whether that node was on the control arm (``held_out``).
-- Nothing turned those records into evidence. This migration adds the two tables
-- that hold the tally:
--
--   context_item_utility
--     item_id   TEXT PRIMARY KEY   pack item id, exactly as it appears in the ledger
--     uses      INTEGER NOT NULL DEFAULT 0   delivered runs that named the item
--     wins      INTEGER NOT NULL DEFAULT 0   of those, the runs that published
--     last_used INTEGER                      epoch seconds of the last tally
--
--   context_item_runs
--     run_id     TEXT PRIMARY KEY   the run whose ledger was already tallied
--     counted_at INTEGER NOT NULL   epoch seconds of the tally; no default, so an
--                                   untimestamped row cannot be hand-written
--
-- ``mini_ork/learning/item_utility.py`` creates the same tables at runtime (the
-- 0073/0078/0079 precedent), so a state.db that never runs this migration still
-- works; the two texts must not drift apart. ``CREATE TABLE IF NOT EXISTS`` makes
-- a re-run a no-op. ADDITIVE only: no existing table or column is touched.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS context_item_utility (
    item_id   TEXT PRIMARY KEY,
    uses      INTEGER NOT NULL DEFAULT 0,
    wins      INTEGER NOT NULL DEFAULT 0,
    last_used INTEGER
);

CREATE TABLE IF NOT EXISTS context_item_runs (
    run_id     TEXT PRIMARY KEY,
    counted_at INTEGER NOT NULL
);

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_migrations(filename, applied_at, checksum)
VALUES ('0082_context_item_utility.sql',
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        'context-item-utility-v1');
