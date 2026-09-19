# Wave: drive book to all-chapters-done at quality, under cost + time ceilings

One wave of the long-horizon PERFORMANCE goal loop against the researcher book
pipeline. It is the measured superset of `kickoffs/book-goal-loop/`: same book,
same units (chapters), same child recipe (`code-fix`), plus the cost and speed
axes the correctness loop cannot see.

## Objective

Every chapter of the book reaches `committed_complete=true` AND
`rubric_status=pass` in `book_chapter_lifecycle` (the correctness bar —
evaluated FIRST and never relaxed), AND does so at-or-under its measured cost
and wall-clock ceilings. The per-chapter contract is enforced by
`binding/unit_predicate.py`, three axes ALL required:

1. QUALITY — `committed_complete=true AND rubric_status='pass'` (hard).
2. COST — `sum(cost_usd)` of `verified-artifact` `task_runs` since the current
   generation epoch, vs the chapter's cost ceiling.
3. SPEED — wall-clock of the most recent attempt (`started_at` → `finished_at`,
   fall back to `committed_at` then `updated_at`), vs the chapter's seconds
   ceiling.

This wave reads per-chapter goal state (watch), picks the failing chapters
(hunt), plans bounded fix children that reduce the dominant node's token cost /
wall-clock in the researcher compose pipeline (fix), and re-checks every
chapter to emit `panel-verdict.json` (verify).

## Definition of Done

`panel-verdict.json` is written with `verdict: pass` iff EVERY chapter unit
satisfies the three-axis predicate. A chapter failing any axis is a failing
unit; `verdict: fail` lists the failing chapter ids and their axis reasons. A
failing wave is a VALID wave — the outer driver re-runs waves, and the operator
tightens the ceilings with `binding/baseline.py --tighten PCT`.

## Files in scope

This wave itself edits NO files — it is config-driven orchestration. The goal
knobs are CONFIG in the `MO_GOAL_*` environment (target repo, units command,
predicate command, evidence command, child recipe, max children per wave); copy
them verbatim into `plan.json`, do not invent them. The fix CHILDREN it
dispatches are scoped to `server/` in the researcher target repo
(`MO_GOAL_TARGET_CWD`). The perf budget lives at
`${MO_GOAL_TARGET_CWD}/.mini-ork/perf-budget.json` and is written only by
`binding/baseline.py` (never by the wave).

## Success Criteria

The wave succeeds when `panel-verdict.json` exists and its `verdict` /
`failing_units` reflect live chapter status across all three axes. A `fail`
verdict is a valid wave outcome (the outer driver re-runs); `verdict: pass`
proves the whole book is done at quality AND under the current cost + time
ceilings.

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
