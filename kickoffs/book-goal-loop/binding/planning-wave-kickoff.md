# Wave: un-wedge researcher DSP planning bursts

One wave of the long-horizon goal loop against the researcher PLANNING stage —
the planning-phase twin of the chapter wave.

## Objective

Every unit the wedge detector lists leaves the planning region. A unit is a
`book_generation_runs.id` (run UUID) whose DSP planning burst has failed with an
IDENTICAL signature on repeated attempts while its heartbeat stays fresh —
thrashing, not merely slow. This wave reads the detector's unit list (watch),
plans a bounded fix child against the researcher compose pipeline (fix), then
re-checks every unit to emit `panel-verdict.json` (verify).

## Definition of Done

`panel-verdict.json` is written with `verdict: pass` iff NO unit is still in the
planning region (`plan` / `plan_sketching` / `failed`); otherwise `verdict: fail`
with the still-wedged run ids listed. A failing wave is a VALID wave — the outer
driver re-runs waves.

An empty unit list is a PASS with zero units: it means nothing is wedged, which
is the goal state, not a missing signal.

## Files in scope

This wave itself edits NO files — it is config-driven orchestration. The goal
knobs are CONFIG in the `MO_GOAL_*` environment (target repo, units command,
predicate command, child recipe, max children per wave); copy them verbatim into
`plan.json`, do not invent them. The fix CHILDREN it dispatches are scoped to
`server/` in the researcher target repo (`MO_GOAL_TARGET_CWD`).

## Success Criteria

The wave succeeds when `panel-verdict.json` exists and its `verdict` /
`failing_units` reflect live FSM status. A `fail` verdict is a valid wave
outcome (the outer driver re-runs); `verdict: pass` proves no planning burst is
wedged.

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
