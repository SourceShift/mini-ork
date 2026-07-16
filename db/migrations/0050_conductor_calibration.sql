-- mini-ork migration 0050 — make the conductor falsifiable
--
-- THE BUG: the conductor predicts a score for every decision and never records what it
-- actually got. Measured on a live db: 10 decisions, 10 with predicted_score, ZERO with
-- realized_score. It is uncalibrated BY CONSTRUCTION — it cannot learn from its own choices,
-- and no UI can ever answer "was the recommendation right?", because nothing wrote the answer.
--
-- WHY the existing write-back never fires. mini_ork_execute.py reconciles like this:
--
--     SELECT cd.id, e.status FROM conductor_decisions cd
--       JOIN epics e ON e.id = cd.epic_id
--      WHERE COALESCE(cd.outcome,'pending')='pending' AND e.status IN ('done','escalated')
--
-- It waits on an EPIC reaching a terminal state. But a `bin/mini-ork run` completes a
-- TASK_RUN and does not necessarily advance any epic — on the live db all 10 decisions point
-- at epic `libwit-se-1`, whose status is still `not started`. For run-driven work the
-- reconciliation can never fire. The outcome is unreachable, not merely unwritten.
--
-- FIX 1 — link a decision to the RUN it produced (task_run_id), so realized_score can be
-- reconciled from the thing that actually finishes. The epic path stays as a fallback for
-- epic-driven work; this adds the run path that was missing.
--
-- FIX 2 — record what was PROPOSED alongside what was CHOSEN, and who chose it.
--
-- The second is the more valuable one. When a human overrides the conductor and the run is
-- then verified, that is a LABELLED EXAMPLE: "the system proposed X, a human chose Y, and Y
-- turned out better/worse". That is the highest-value training signal in the product, and
-- today it has nowhere to go — conductor_decisions records only the final choice, so a human
-- correction is indistinguishable from the machine agreeing with itself.
--
-- Depends on: the table created in an earlier core/evolution migration.
-- Apply via: mini-ork init OR sqlite3 $MINI_ORK_DB < db/migrations/0050_conductor_calibration.sql

BEGIN;

-- ── FIX 1: reconcile against the run, not only the epic ──────────────────────
ALTER TABLE conductor_decisions ADD COLUMN task_run_id TEXT;

-- ── FIX 2: proposed vs chosen — the difference IS the training signal ────────
ALTER TABLE conductor_decisions ADD COLUMN proposed_topology TEXT;
ALTER TABLE conductor_decisions ADD COLUMN proposed_recipe TEXT;
ALTER TABLE conductor_decisions ADD COLUMN proposed_lane_hints TEXT;

-- 'conductor' when the machine's proposal was taken unchanged.
-- 'human' when a person overrode it in the topology composer.
-- Defaulting to 'conductor' is correct for the 10 historical rows: they were never
-- surfaced to a human, so nobody could have overridden them.
ALTER TABLE conductor_decisions ADD COLUMN decided_by TEXT NOT NULL DEFAULT 'conductor';

-- Why a human overrode it, when they say. Free text; often the most useful column in the
-- table, because it is the only place the system learns a preference it could not measure.
ALTER TABLE conductor_decisions ADD COLUMN override_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_conductor_decisions_task_run
  ON conductor_decisions(task_run_id);

-- Open decisions awaiting an outcome. The reconciler reads this; the UI reads it to show
-- "N recommendations still unproven".
CREATE INDEX IF NOT EXISTS idx_conductor_decisions_pending
  ON conductor_decisions(outcome, task_run_id);

COMMIT;
