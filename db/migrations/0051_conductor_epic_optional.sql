-- mini-ork migration 0051 — a conductor decision does not require an epic
--
-- `conductor_decisions.epic_id` was NOT NULL. That encodes the same wrong assumption that
-- caused the calibration bug 0050 fixes: it presumes every decision belongs to an epic.
--
-- It does not. `bin/mini-ork run <kickoff>` produces a TASK_RUN with no epic at all, and that
-- is the majority of dispatches — including every run the topology composer will ever start.
-- With epic_id NOT NULL, recording such a decision is impossible: the insert fails outright,
-- so the run happens and the decision is simply never written down. The system would keep
-- making choices it never records, which is how it got uncalibrated in the first place.
--
-- SQLite cannot drop a NOT NULL constraint in place, so this is the standard table rebuild.
--
-- Depends on: 0050_conductor_calibration.sql (adds task_run_id, proposed_*, decided_by,
--             override_reason — all preserved here).
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0051_conductor_epic_optional.sql

PRAGMA foreign_keys=OFF;

BEGIN;

CREATE TABLE conductor_decisions_new (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at          INTEGER,
    -- NULLABLE now: a run-driven dispatch has no epic, and that is normal, not an error.
    epic_id             TEXT,
    task_class          TEXT,
    task_run_id         TEXT,

    -- what the machine suggested
    proposed_topology   TEXT,
    proposed_recipe     TEXT,
    proposed_lane_hints TEXT,

    -- what was actually run
    chosen_topology     TEXT,
    chosen_recipe       TEXT,
    chosen_lane_hints   TEXT,

    -- who decided, and why they disagreed
    decided_by          TEXT NOT NULL DEFAULT 'conductor',   -- 'conductor' | 'human'
    override_reason     TEXT,

    predicted_score     REAL,
    budget_pct_used     REAL,
    rationale           TEXT,

    -- what actually happened (0050: now reachable via task_run_id)
    outcome             TEXT,
    realized_score      REAL
);

INSERT INTO conductor_decisions_new (
    id, decided_at, epic_id, task_class, task_run_id,
    proposed_topology, proposed_recipe, proposed_lane_hints,
    chosen_topology, chosen_recipe, chosen_lane_hints,
    decided_by, override_reason,
    predicted_score, budget_pct_used, rationale, outcome, realized_score
)
SELECT
    id, decided_at, epic_id, task_class, task_run_id,
    proposed_topology, proposed_recipe, proposed_lane_hints,
    chosen_topology, chosen_recipe, chosen_lane_hints,
    COALESCE(decided_by, 'conductor'), override_reason,
    predicted_score, budget_pct_used, rationale, outcome, realized_score
FROM conductor_decisions;

DROP TABLE conductor_decisions;
ALTER TABLE conductor_decisions_new RENAME TO conductor_decisions;

CREATE INDEX IF NOT EXISTS idx_conductor_decisions_task_run
  ON conductor_decisions(task_run_id);
CREATE INDEX IF NOT EXISTS idx_conductor_decisions_pending
  ON conductor_decisions(outcome, task_run_id);

COMMIT;

PRAGMA foreign_keys=ON;
