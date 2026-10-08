# A recovered run keeps its task class (it silently became "generic" and failed the wrong gates)

## Why (live, 2026-10-08)

Reviving `ide-orca-b2a-triage-20261008104922` (code-fix) with `mini-ork recover … --from-node
test` re-ran test → reviewer (pass) → eval → publisher. The publisher then logged
`[BLOCK] oracle-gates: safety_violation — publish refused`.

The cause:
- `execute.main` resolves the task class from `plan.json` `task_class`, then the
  `MINI_ORK_TASK_CLASS` env var, then `"generic"` (`mini_ork/cli/execute.py` ~:827-835).
- This run's `plan.json` has no `task_class`. The normal run flow sets the env var
  (`mini_ork/cli/main.py` ~:885), but `recover` (`mini_ork/recovery/planner.py` `cli_main`) does
  not. So the recovered execute ran as `generic`, and the publisher evaluated the `generic` gate
  set.
- Reproduced read-only: `gate_run_all(..., task_class="generic")` gives
  `oracle-synthesis-promote: fail` (a safety gate) → `safety_violation: True`;
  `task_class="code_fix"` gives no fail.
- The task class IS known: `task_runs.task_class = 'code_fix'` and `run_profile.json`
  `task_class = 'code_fix'`.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/planner.py`: ONLY `cli_main`, the env hand-off block that sets
  `MINI_ORK_RUN_ID` / `MINI_ORK_RUN_DIR` / `MINI_ORK_WORKFLOW` / `MINI_ORK_RECIPE`
- `mini_ork/cli/execute.py`: ONLY the task-class resolution in `main` (~:827-835)
- `tests/unit/test_recover_task_class.py` (new)

## Changes (exact)

1. **`cli_main` hand-off.** Add `"MINI_ORK_TASK_CLASS": <task class>` to the
   `apply_env_overrides` call. Resolve the value in this order:
   1. the `task_runs.task_class` row for the run (`handoff["db_path"]`);
   2. `<run_dir>/run_profile.json` `task_class`;
   3. `plan.json` `task_class`;
   4. otherwise leave it unset.

   Never override an explicitly set non-empty `MINI_ORK_TASK_CLASS`.
2. **`execute.main` resolution chain** (defence in depth, for any other entry point):
   1. `plan.json` `task_class`;
   2. the `MINI_ORK_TASK_CLASS` env;
   3. the run's `run_profile.json` `task_class` (the run dir is next to the plan);
   4. the `task_runs.task_class` row when the DB and run id are known;
   5. `"generic"`.

   Read-only and fail-soft.

## Tests (`tests/unit/test_recover_task_class.py`)

- `cli_main` with a stub `execute_fn` and a temp DB whose `task_runs` row has
  `task_class='code_fix'`, and a `plan.json` without `task_class`: inside the stub,
  `MINI_ORK_TASK_CLASS == "code_fix"`.
- An explicit `MINI_ORK_TASK_CLASS=other` in the env is kept.
- The execute resolution helper (extract it as `_resolve_task_class(plan_path, run_id, db)` so
  it is testable): no plan class, no env → it reads `run_profile.json`; nothing anywhere →
  `"generic"`.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_recover_task_class.py tests/unit/test_recover_revival.py tests/unit/test_recover_cli_dispatch.py tests/unit/test_recover_verify.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME -u MINI_ORK_TASK_CLASS python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/recovery/planner.py mini_ork/cli/execute.py tests/unit/test_recover_task_class.py` → clean.
- `git diff --stat` touches only the files in scope.
