# Implementer reward = the run verified, minus a cost penalty (env-gated, default off)

## Goal

The router learns which lane to give each role from `execution_traces.reward_g`. For the
code-fix implementer that reward is effectively "produced a diff": on the 40-task
held-out baseline, 61 of 64 implementer rows scored +1 while only 22 of 40 tasks were
actually resolved, because test, typecheck and reviewer failures downstream are never
credited back to the implementer. Cost only enters routing as a ±0.1 tie-break on
exactly flat groups, read from a single row. So the router can neither learn "this lane's
fixes verify less often" nor "this lane is cheaper for the same verification".

Add an opt-in reward that stamps the implementer row with whether the run passed its OWN
checks (never anything external), minus a cost penalty.

## Files in scope

- `mini_ork/cli/execute.py` — pass `fail_count` into `_post_run_learning`; add step 0.
- `mini_ork/learning/writeback.py` — new `stamp_impl_run_verified`.
- `tests/unit/test_impl_run_verified_reward.py` — NEW.

No other file changes.

## Mechanism (exact spec)

### `mini_ork/cli/execute.py`

- The call `_post_run_learning(db, live_run_dir, run_id, task_class)` (after
  `_emit_run_verdict`) becomes `_post_run_learning(db, live_run_dir, run_id, task_class, fail_count=fail_count)`.
- `_post_run_learning(db, run_dir, run_id, task_class="", fail_count=None)`. Add a step 0
  before the rubric fill, best-effort like the other steps (wrapped in try/except, never
  fails the run):
  ```python
  if os.environ.get("MO_IMPL_REWARD", "") == "run_verified" and fail_count is not None:
      from mini_ork.learning import writeback as _wb
      _wb.stamp_impl_run_verified(db, run_id, verified=(fail_count == 0))
  ```
  Update the docstring list with step 0. `fail_count == 0` means every dispatched node
  (typecheck, test, reviewer, publisher …) succeeded — in-run signals only.

### `mini_ork/learning/writeback.py`

```python
def stamp_impl_run_verified(db, run_id: str, verified: bool) -> int:
    """Returns the number of implementer rows stamped (0 when none / infra)."""
```

- Open the DB the same way the other writeback helpers in this module do (accept the
  same `db` argument shape they accept).
- Select this run's implementer rows from `execution_traces`:
  `run_id = ?` and `json_extract(verifier_output, '$.node_type') = 'implementer'`.
  None → return 0.
- Infra check: if ANY `execution_traces` row of this run has a non-empty `validity` other
  than `'valid'` → set `validity = 'infra_failed'` on the implementer rows, return 0
  (`_row_is_valid` already drops non-valid rows from learning).
- Otherwise for each implementer row:
  `lam = float(os.environ.get("MO_ROUTER_COST_LAMBDA", "0"))`,
  `ref = float(os.environ.get("MO_ROUTER_COST_REF", "0.20"))` (ref <= 0 → treat as 0.20),
  `cost = row cost_usd or 0.0`,
  `v = (1.0 if verified else 0.0) - lam * cost / ref`, clamped to [-1.0, 1.0];
  write `reward_value = v`, `reward_anchor = 0.5`, `reward_g = (v - 0.5) / 0.5`,
  `reward_source = 'run_verified@v1'`. Only columns that exist in the table are written
  (check `PRAGMA table_info(execution_traces)` once; skip missing ones — older DBs).
- Commit; return the count.

Because the stamp runs before step 3 (`lane_router.recompute_advantages`), the
domain/region advantage tables reflect it in the same run.

## Tests (`tests/unit/test_impl_run_verified_reward.py`)

Build a temp DB with the repo's migrations (follow how existing writeback / lane_router
tests create one — `grep -rl "execution_traces" tests/unit | head`) and insert rows
directly.

- flag unset → `_post_run_learning` leaves implementer rows untouched.
- `stamp_impl_run_verified(verified=True)`, λ=0 → `reward_value == 1.0`, `reward_g == 1.0`,
  `reward_source == 'run_verified@v1'`.
- `verified=False`, λ=0 → `reward_value == 0.0`, `reward_g == -1.0`.
- λ=0.1, ref=0.20, two runs with verified=True and costs 0.05 vs 0.15 → the cheaper row's
  `reward_g` is higher by exactly `0.1 * 0.10 / 0.20 / 0.5` (= 0.1).
- a run with another row `validity='timeout'` → implementer rows become
  `validity='infra_failed'`, rewards untouched, return 0.
- no implementer rows → returns 0, nothing written.
- non-implementer rows of the run are never modified.
- `_post_run_learning(..., fail_count=0)` with `MO_IMPL_REWARD=run_verified` and the
  other steps disabled (`MO_GRADE_RUN_REWARD=0 MO_LEARNING_WRITEBACK=0 MO_LANE_ROUTER=0`)
  stamps the row; with `fail_count=None` it does not.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_impl_run_verified_reward.py` passes, plus every
  existing test touching `_post_run_learning` or `writeback` (`grep -rl "_post_run_learning\|learning.writeback\|write_grpo_advantages" tests/`).
- `ruff check` clean on the touched files.
- Diff touches only the three files in scope.
