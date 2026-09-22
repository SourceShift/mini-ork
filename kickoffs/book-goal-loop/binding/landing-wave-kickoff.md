# Wave: land the pinned compose job into the writing region

One wave of the long-horizon goal loop against a compose job parked BEFORE the
planning region — the draft-stage sibling of the planning wave. The unit is
pinned by the operator (`MO_GOAL_LANDING_RUN_UUID`), because a job wedged at
`draft` produces no burst events and no lifecycle rows, so no statistical
detector can see it.

## Objective

The pinned `book_generation_runs.id` reaches the WRITING region
(`generating` / `completed`). This wave reads the pinned lister (watch), plans
a bounded fix child against the researcher compose pipeline (fix), then
re-checks the unit to emit `panel-verdict.json` (verify). After a live deploy,
the re-dispatch fires the next golden-path FSM action
(`draft→next`, `plan→start_planning`, `plan_ready→confirm_plan`, …) through
the product's own sanctioned pipeline; if the action is REFUSED, that refusal
is next wave's sharpest evidence.

## Definition of Done

`panel-verdict.json` is written with `verdict: pass` iff the unit's FSM is in
`generating` / `completed`; otherwise `verdict: fail` with the run id listed.
A failing wave is a VALID wave — the outer driver re-runs waves.

An empty unit list is a PASS with zero units: the pinned job entered the
writing region, which is the goal state, not a missing signal (the lister
exits 2 when it cannot read the DB, so empty is never a blind spot).

## Files in scope

This wave itself edits NO files — it is config-driven orchestration. The goal
knobs are CONFIG in the `MO_GOAL_*` environment (target repo, units command,
predicate command, child recipe, max children per wave); copy them verbatim
into `plan.json`, do not invent them. The fix CHILDREN it dispatches are
scoped to `server/` in the researcher target repo (`MO_GOAL_TARGET_CWD`).

## Success Criteria

The wave succeeds when `panel-verdict.json` exists and its `verdict` /
`failing_units` reflect live FSM status. A `fail` verdict is a valid wave
outcome (the outer driver re-runs); `verdict: pass` proves the job is writing.

## Proof command

```
test -f "${MINI_ORK_RUN_DIR}/panel-verdict.json"
```

That file is written by the `goal_check` verifier
(`recipes/goal-loop/verifiers/goal_check.py`), which re-runs the units lister +
predicate over every unit. Its presence proves the wave completed its
watch→fix→verify cycle and recorded a verdict.

## Model Preference

Planner/publisher on cheap lanes; fix children use `minimax`/`codex` (never glm).
