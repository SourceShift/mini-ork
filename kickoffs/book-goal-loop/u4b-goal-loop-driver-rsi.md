# U4b goal-loop driver: cross-wave loop + RSI stops (GRAO, UCCI, divergence-kill)

## Goal

- Build the outer loop that re-runs the `goal-loop` recipe (built in U4a) wave after
  wave until the goal predicate holds everywhere or an RSI stop fires. Pure Python,
  upstream, goal-agnostic. Two new modules under `recipes/goal-loop/lib/` plus a driver
  entry.
- `recipes/goal-loop/lib/loop_state.py` — persistent cross-wave memory in a single JSON
  file `<state_dir>/goal-loop-state.json` (state_dir passed in, default
  `${MINI_ORK_HOME:-.mini-ork}/goal-loop/<goal_id>/`):
  - `waves`: list of `{wave, run_id, failing_before, failing_after, cost_usd}`;
  - `failed_fixes`: GRAO outcome-tagged memory — map `unit_id -> [fix_attempt_hash]`
    where the hash is sha256 of (unit_id + sorted failing reasons). A unit whose CURRENT
    failure hash already appears twice is QUARANTINED (skipped by hunt, reported in the
    final verdict as `quarantined_units`) instead of burning further spend;
  - `failure_signature`: sha256 of the sorted failing-unit list per wave —
    divergence-kill fires when the same signature repeats for two consecutive waves
    (no progress) OR when `failing_after > failing_before` two waves in a row
    (regressing);
  - pure functions, no globals: `load_state`, `save_state`, `record_wave`,
    `should_quarantine(unit, current_hash)`, `divergence(state) -> str | None`.
- `recipes/goal-loop/lib/drive.py` — the loop driver:
  - `drive(goal_id, target_cwd, units_cmd, predicate_cmd, child_recipe, *, max_waves,
    budget_total_usd, run_wave_fn, cost_fn, state_dir) -> dict` — dependency-injected:
    `run_wave_fn(wave_no, quarantined: set[str]) -> dict` executes ONE recipe run and
    returns its parsed panel-verdict.json; `cost_fn() -> float` returns cumulative spend
    (production wiring reads the runs DB `today_cost_usd`; tests inject fakes).
  - Loop: while waves < max_waves: run wave → record → stop conditions in priority
    order: (1) verdict pass → return `{"stop": "goal_met", ...}`; (2) UCCI budget stop:
    projected next-wave cost (mean of last 2 wave costs) would exceed
    `budget_total_usd` → `{"stop": "budget"}`; (3) divergence-kill →
    `{"stop": "diverged", "signature": ...}`; (4) all remaining failures quarantined →
    `{"stop": "all_quarantined"}`; else next wave.
  - `main(argv)` CLI: `python3 -m recipes... ` is NOT importable — instead expose
    `recipes/goal-loop/lib/drive.py` runnable as a script
    (`python3 recipes/goal-loop/lib/drive.py --goal-id ... --target-cwd ...
    --units-cmd ... --predicate-cmd ... --child-recipe ... --max-waves 30
    --budget-usd 150`), where the default `run_wave_fn` shells out to
    `bin/mini-ork run goal-loop <kickoff>` with the env config exported and reads
    `panel-verdict.json` from the run dir; default `cost_fn` returns 0.0 when the runs
    DB is unavailable (budget stop then relies on max_waves).
  - Every stop reason is WRITTEN to `<state_dir>/final-verdict.json` as
    `{"stop": ..., "waves": N, "failing_units": [...], "quarantined_units": [...]}`.
- Update `recipes/goal-loop/register.py`: replace the stub `goal_sweep` submode body so
  it reads `sweep-plan.json` and for each planned unit invokes
  `mini_ork.cli.spawn` (`bin/mini-ork spawn`-equivalent API) with
  `--recipe $MO_GOAL_CHILD_RECIPE`, honoring `MINI_ORK_RECURSIVE_MAX_PARALLEL`; if the
  spawn module refuses (caps), mark the unit `deferred` in `sweep-result.json`. Keep it
  import-safe; all spawn calls go through a `spawn_fn` injected via env-guarded default
  so the unit test can fake it (`MO_GOAL_SPAWN_DRY=1` → record only).
- Unit test `tests/unit/test_goal_loop_driver.py` (fakes only, NO LLM, NO subprocess to
  bin/mini-ork):
  - goal met on wave 2 → stop goal_met, state file has 2 waves;
  - same failure signature two waves running → stop diverged;
  - regressing failing count two waves running → stop diverged;
  - budget: wave costs 5.0 + 5.0 with budget 12 → stop budget before wave 3;
  - quarantine: unit failing with identical hash across 2 waves is excluded from wave 3
    hunt set and listed in final verdict quarantined_units;
  - `MO_GOAL_SPAWN_DRY=1` sweep submode records planned spawns without calling spawn.
- Size: **M**. No executor-core edits; only the four in-scope files.

## Files in scope

- `recipes/goal-loop/lib/loop_state.py` (new)
- `recipes/goal-loop/lib/drive.py` (new)
- `recipes/goal-loop/register.py` (edit — replace stub submode body)
- `tests/unit/test_goal_loop_driver.py` (new)

## Definition of Done

- All six test scenarios above green.
- `drive()` is fully injectable (run_wave_fn/cost_fn/state_dir) — zero network, zero
  LLM in tests.
- Stop-priority order is exactly: goal_met > budget > diverged > all_quarantined.
- final-verdict.json written on every stop path.
- U4a tests still green (`tests/unit/test_goal_loop_recipe.py`).

## Verification commands

- `python3 -m pytest tests/unit/test_goal_loop_driver.py tests/unit/test_goal_loop_recipe.py -q`
- `python3 -m ruff check recipes/goal-loop/lib/loop_state.py recipes/goal-loop/lib/drive.py recipes/goal-loop/register.py --select F,E9`

## Done When

- All verification commands pass in the isolated worktree.
- Diff touches only the four in-scope files.
