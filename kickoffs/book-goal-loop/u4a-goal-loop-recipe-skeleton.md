# U4a goal-loop recipe skeleton: goal-agnostic watch/hunt/fix/verify wave

## Goal

- New upstream recipe `recipes/goal-loop/` — ONE run of it executes ONE wave of a
  long-horizon goal loop against a target repo: read per-unit goal state (watch), pick
  failing units (hunt), dispatch bounded fix children (fix), re-check touched units and
  record merge decisions (verify), then emit `panel-verdict.json` with
  `"verdict": "pass"` iff EVERY unit satisfies the goal predicate. An outer driver
  (built separately in U4b) re-runs waves until pass/stop — this kickoff builds only the
  recipe skeleton and its contracts.
- Everything goal-specific is CONFIG, read from env by the recipe's nodes:
  - `MO_GOAL_TARGET_CWD` — target repo the goal is about (absolute path).
  - `MO_GOAL_UNITS_CMD` — command run inside target cwd printing one unit id per line
    (e.g. a glob lister for chapter files).
  - `MO_GOAL_PREDICATE_CMD` — command run inside target cwd, given a unit id as `$1`,
    exiting 0 (pass) / non-zero (fail) and printing a one-line reason.
  - `MO_GOAL_CHILD_RECIPE` — recipe name for fix children (dispatched per failing unit).
  - `MO_GOAL_MAX_CHILDREN_PER_WAVE` — int, default 3.
- Files to create under `recipes/goal-loop/`:
  1. `task_class.yaml` — `task_class: goal_loop`, mirroring the shape of
     `recipes/chapter-validation-10lens/task_class.yaml`.
  2. `workflow.yaml` — nodes: `planner` (type planner, serial) →
     `goal_state` (type transform, serial; runs UNITS + PREDICATE commands, writes
     `goal-state.json`: `{unit_id: {"pass": bool, "reason": str}}`) →
     `sweep_dispatcher` (type transform, serial; reads goal-state.json, selects up to
     MAX_CHILDREN failing units, writes `sweep-plan.json` listing
     `{unit_id, child_recipe, kickoff_hint}`; in THIS kickoff it only PLANS —
     actual child spawning arrives with the U4b driver) →
     `goal_check` (type verifier, `verifier_ref: verifiers/goal_check.py`; re-runs the
     predicate over every unit, writes `${MINI_ORK_RUN_DIR}/panel-verdict.json`
     `{"verdict": "pass"|"fail", "failing_units": [...], "total_units": N}`; verifier
     exits 0 in BOTH cases — a failing goal is a valid wave, only missing/undecidable
     state exits non-zero) → `publisher` (type publisher; wave report). Real
     `depends_on` edges chaining those five nodes; recursion block with
     `max_iterations: 30`, `convergence_check: all_goal_units_pass`,
     `budget_cap_per_iter_usd: 10.00`, `budget_cap_total_usd: 150.00`, and a
     `divergence_kill` sentence (schema now locks these five keys — see
     `schemas/workflow.schema.json`).
  3. `register.py` — module-level side effects only; MUST be import-safe when
     mini_ork is importable. For the skeleton register a no-op marker:
     `from mini_ork.cli.execute_handlers import register_implementer_submode` and
     register submode `"goal_sweep"` with a stub function returning a dict
     `{"status": "planned_only"}`. This dogfoods the new recipe-local loader
     (`mini_ork/cli/recipe_register.py`).
  4. `prompts/planner.md`, `prompts/publisher.md` — short, generic wave prompts.
  5. `lib/goal_state.py` — pure functions `list_units(target_cwd, units_cmd) ->
     list[str]` and `evaluate_units(target_cwd, predicate_cmd, units) ->
     dict[str, dict]` via subprocess, no shell=True string interpolation of unit ids
     (pass unit as argv element).
  6. `verifiers/goal_check.py` — implements goal_check as described; reads env config;
     tolerates zero units by emitting verdict fail with reason `no_units`.
- Unit test `tests/unit/test_goal_loop_recipe.py`:
  - workflow.yaml parses, validates the recursion block against
    `schemas/workflow.schema.json`, and compiles via
    `mini_ork.workflow.compiler.compile_workflow` with the 4-edge chain visible in
    `control_parents`;
  - `load_recipe_register(Path('recipes/goal-loop'))` returns True and the
    `goal_sweep` submode appears in the submode registry;
  - `lib/goal_state.evaluate_units` with a fake predicate script (tmp_path shell stub)
    classifies pass/fail units correctly;
  - running `verifiers/goal_check.py` with a stub predicate over 2 units (1 fail)
    writes panel-verdict.json with verdict fail + failing_units list; with 0 failing
    units writes verdict pass.
- Size: **M** but skeleton-only — NO child spawning, NO loop driver, NO GRAO/UCCI logic
  in this kickoff (that is U4b). Keep every module small and pure.

## Files in scope

- `recipes/goal-loop/task_class.yaml` (new)
- `recipes/goal-loop/workflow.yaml` (new)
- `recipes/goal-loop/register.py` (new)
- `recipes/goal-loop/prompts/planner.md` (new)
- `recipes/goal-loop/prompts/publisher.md` (new)
- `recipes/goal-loop/lib/goal_state.py` (new)
- `recipes/goal-loop/lib/__init__.py` (new, empty)
- `recipes/goal-loop/verifiers/goal_check.py` (new)
- `tests/unit/test_goal_loop_recipe.py` (new)

## Definition of Done

- All nine files exist; no file outside `recipes/goal-loop/` + the one test file is
  touched.
- workflow.yaml recursion block validates against the locked schema.
- register.py loads through `load_recipe_register` and registers the `goal_sweep`
  submode (proven by test).
- goal_check writes panel-verdict.json in the exact shape above for pass, fail, and
  no-units cases (proven by test).
- All tests green.

## Verification commands

- `python3 -m pytest tests/unit/test_goal_loop_recipe.py -q`
- `python3 -m pytest tests/unit/test_recipe_register.py tests/unit/test_workflow_schema_recursion.py -q`
- `python3 -c "import yaml; yaml.safe_load(open('recipes/goal-loop/workflow.yaml'))"`

## Done When

- All verification commands pass in the isolated worktree.
- Diff touches only the in-scope files.
