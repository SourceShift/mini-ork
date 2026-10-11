# U4d — goal-loop: separate the wave's own kickoff from the child kickoff

## Goal

`_default_run_wave_fn` in `recipes/goal-loop/lib/drive.py` shells
`bin/mini-ork run goal-loop <kickoff>` and currently uses `MO_GOAL_CHILD_KICKOFF`
as that `<kickoff>` argument. But `MO_GOAL_CHILD_KICKOFF` is the CHILD's kickoff
— the per-unit fix template that `_default_spawn_fn` templates `{{unit_id}}` /
`{{reason}}` into (U4c). Feeding the child's editorial "edit this ONE file"
template to the WAVE's own goal-loop run is a category error: the wave kickoff
should describe the WAVE goal (evaluate all units, plan fixes, re-check), not
instruct an in-place edit of a literal `{{unit_id}}` path.

Add a dedicated `MO_GOAL_WAVE_KICKOFF` env var for the wave's own kickoff, with
`MO_GOAL_CHILD_KICKOFF` as the backward-compatible fallback.

Do NOT scrub `MO_STATIC_RECIPE_PLAN` from the child spawn env. Investigation of
`mini_ork/cli/plan.py:655` + `mini_ork/planning/recipe_plan.py:recipe_fallback_plan`
shows that under `MO_STATIC_RECIPE_PLAN=1` the plan is rendered deterministically
from the recipe's `workflow.yaml` and the implementer node still reads its
kickoff at dispatch (the plan JSON never carries kickoff content). code-fix is a
FIXED recipe, so its static plan IS the intended DAG — leaving the flag ON for
children is correct AND saves one planner LLM call per child. Touch nothing about
the spawn env.

## Mechanism (exact spec)

### `recipes/goal-loop/lib/drive.py` — `_default_run_wave_fn` only

Replace the single wave-kickoff resolution line. Current:

```python
    kickoff = os.environ.get("MO_GOAL_CHILD_KICKOFF")
    if not kickoff:
        raise RuntimeError("MO_GOAL_CHILD_KICKOFF must be set for default run_wave_fn")
```

New (prefer the wave kickoff, fall back to the child kickoff for zero regression):

```python
    kickoff = os.environ.get("MO_GOAL_WAVE_KICKOFF") or os.environ.get("MO_GOAL_CHILD_KICKOFF")
    if not kickoff:
        raise RuntimeError(
            "MO_GOAL_WAVE_KICKOFF or MO_GOAL_CHILD_KICKOFF must be set for default run_wave_fn"
        )
```

Change ONLY these lines inside `_default_run_wave_fn`. Do not touch
`_default_spawn_fn`, `sweep_run`, `_default_cost_fn`, or any other function.

## Tests — `tests/unit/test_goal_loop_driver.py` (extend, keep all green)

- `MO_GOAL_WAVE_KICKOFF` preference: with BOTH `MO_GOAL_WAVE_KICKOFF` and
  `MO_GOAL_CHILD_KICKOFF` set to two distinct paths and `MINI_ORK_RUN_DIR` set to
  a tmp dir, monkeypatch `subprocess.run` (in the `drive` module) to capture its
  first positional arg (the argv list); call `drive_mod._default_run_wave_fn(1, set())`;
  assert the captured argv is `[<cli>, "run", "goal-loop", <WAVE_KICKOFF path>]` —
  i.e. the WAVE kickoff, NOT the child kickoff. Stub the panel-verdict read path so
  the function returns without error (no panel-verdict.json present → payload keeps
  `verdict: fail`, which is fine to assert alongside the argv).
- Fallback: with `MO_GOAL_WAVE_KICKOFF` UNSET and only `MO_GOAL_CHILD_KICKOFF`
  set, the same capture shows the argv uses the CHILD kickoff path (backward
  compatibility preserved).
- Unset-both: with neither var set, `_default_run_wave_fn(1, set())` raises
  `RuntimeError` mentioning both var names.

## Files in scope

- recipes/goal-loop/lib/drive.py (ONLY the wave-kickoff lines in `_default_run_wave_fn`)
- tests/unit/test_goal_loop_driver.py

## Verification commands

- `python3.11 -m pytest tests/unit/test_goal_loop_driver.py tests/unit/test_goal_loop_recipe.py -q` → all green
- `python3.11 -c "import ast; src=open('recipes/goal-loop/lib/drive.py').read(); assert 'MO_GOAL_WAVE_KICKOFF' in src; assert src.count('MO_GOAL_WAVE_KICKOFF')>=1; print('wave kickoff env present')"`

## Done When

- `_default_run_wave_fn` prefers `MO_GOAL_WAVE_KICKOFF`, falls back to
  `MO_GOAL_CHILD_KICKOFF`, and raises naming both when neither is set.
- `_default_spawn_fn` and the child spawn env are UNCHANGED (no MO_STATIC_RECIPE_PLAN scrub).
- Both verification commands pass; no other file is modified.
