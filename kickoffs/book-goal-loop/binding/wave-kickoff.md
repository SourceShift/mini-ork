# Wave: drive book d0df3cdb to all-chapters-done-at-quality

One wave of the long-horizon goal loop against the researcher book pipeline.

## Objective

Every chapter of book `d0df3cdb-8164-450e-b841-2c9354ea0423` reaches
`committed_complete=true` AND `rubric_status=pass` in `book_chapter_lifecycle`,
at the lowest token cost. This wave reads per-chapter goal state (watch), picks
the failing chapters (hunt), plans bounded fix children that patch the
researcher compose pipeline (fix), and re-checks every chapter to emit
`panel-verdict.json` (verify).

## Definition of Done

`panel-verdict.json` is written with `verdict: pass` iff EVERY chapter unit
satisfies the goal predicate; otherwise `verdict: fail` with the failing chapter
ids listed. A failing wave is a VALID wave — the outer driver re-runs waves.

## Files in scope

This wave itself edits NO files — it is config-driven orchestration. The goal
knobs are CONFIG in the `MO_GOAL_*` environment (target repo, units command,
predicate command, child recipe, max children per wave); copy them verbatim into
`plan.json`, do not invent them. The fix CHILDREN it dispatches are scoped to
`server/` in the researcher target repo (`MO_GOAL_TARGET_CWD`).

## Success Criteria

The wave succeeds when `panel-verdict.json` exists and its `verdict` /
`failing_units` reflect live chapter status. A `fail` verdict is a valid wave
outcome (the outer driver re-runs); `verdict: pass` proves the whole book is done
at quality.

## Proof command

```
test -f "${MINI_ORK_RUN_DIR}/panel-verdict.json"
```

That file is written by the `goal_check` verifier
(`recipes/goal-loop/verifiers/goal_check.py`), which re-runs the units lister +
predicate over every chapter. Its presence proves the wave completed its
watch→hunt→fix→verify cycle and recorded a verdict.

## Model Preference

Planner/publisher on cheap lanes; fix children use `minimax`/`codex` (never glm).
