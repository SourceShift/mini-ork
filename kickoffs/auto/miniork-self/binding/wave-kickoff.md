# Wave: close one measured defect in mini-ork itself

One wave of the long-horizon goal loop pointed at mini-ork ITSELF. The unit is
a specific, reproducible defect in this repository, and the predicate that
scores it is an executable probe, not a claim: a wave passes only when the
probe — which is required to stay RED on the unfixed tree — goes GREEN on the
tree the fix child edited.

## Objective

The unit's predicate returns 0. The wave reads the pinned unit list (watch),
plans a bounded `code-fix` child against the target worktree (fix), then
re-checks every unit to emit `panel-verdict.json` (verify). The child's edit is
scored in place: the predicate reads `MO_GOAL_TARGET_CWD`, the worktree the
child edits, so no deploy stage is needed and mini-ork is not asked to
self-modify mid-run. Merging that worktree to `main` stays a separate,
human-gated step.

## Definition of Done

`panel-verdict.json` is written with `verdict: pass` iff every listed unit's
predicate exits 0; otherwise `verdict: fail` with the failing units listed. A
failing wave is a VALID wave — the outer driver re-runs waves, and a fix child
that fails the predicate simply leaves the unit open.

The unit list is STATIC and non-vacuous (`binding/list_units.py` exits 2 rather
than emitting an empty list when the target tree does not look like mini-ork or
the live db is unreadable), so `MO_GOAL_EMPTY_UNITS_PASS` is NOT set: an empty
list here would mean a broken lister, not a satisfied goal.

## Files in scope

This wave itself edits NO files — it is config-driven orchestration. The goal
knobs are CONFIG in the `MO_GOAL_*` environment (target cwd, units command,
predicate command, child recipe, max children per wave); copy them verbatim
into `plan.json`, do not invent them. The fix CHILDREN it dispatches are scoped
to the mini-ork target worktree (`MO_GOAL_TARGET_CWD`) — its `mini_ork/` tree
and `db/migrations/`. Each unit's section in `binding/child-kickoff.md` says
which of those its fix touches.

## The bar is FROZEN

`binding/list_units.py`, `binding/unit_predicate.py` and this directory are
DECLARED PART OF THE INSTRUMENT that scores the work, and are listed in
`MO_GOAL_PROTECTED_PATHS` so a deploy or a child cannot rewrite them. Each
predicate runs at least one probe that MUST go green AND at least one control
that MUST STAY RED — the control feeds the guard the thing it exists to refuse,
so "make the check pass without fixing anything" fails the control and leaves
the unit open. Lowering the bar from inside the tree under test is the one
shortcut this loop is built to refuse.

## Success Criteria

The wave succeeds when `panel-verdict.json` exists and its `verdict` /
`failing_units` reflect the live predicates. A `fail` verdict is a valid wave
outcome (the outer driver re-runs); `verdict: pass` proves the defect is closed
on the target tree.

## Proof command

```
test -f "${MINI_ORK_RUN_DIR}/panel-verdict.json"
```

That file is written by the `goal_check` verifier
(`recipes/goal-loop/verifiers/goal_check.py`), which re-runs the units lister +
predicate over every unit. Its presence proves the wave completed its
watch→fix→verify cycle and recorded a verdict.

## Model Preference

Planner/publisher on cheap lanes; fix children use `minimax` (never glm — it is
analysis-only and 429s silently).
