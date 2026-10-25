# ECOSYSTEM §6 · K1: remove the dead improve → eval → promote path

## Goal

`mini-ork improve`, `mini-ork eval` and `mini-ork promote` form a promotion path
that has never promoted anything:

- `mini_ork/cli/eval.py` calls `benchmark_suite.run(..., runner_fn=None)`, so
  every task is skipped, then hard-codes `total_utility = 0` and
  `utility_delta = 0` (its docstring says it copies a bash bug on purpose).
- `mini_ork/cli/promote.py` refuses any candidate still at
  `status='candidate'`, which is every one of them.
- `mini_ork/learning/group_evolver.py` (imported only by `improve.py`)
  proposes mutations that report `"no-op (preconditions not met)"`.
- `mini-ork run` never calls any of the three; they are standalone
  subcommands only. Every live promotion goes through `mini-ork apply`.

Delete the three subcommands and `group_evolver`. **Keep**
`mini_ork/learning/benchmark_suite.py` (queued epics rsi-e2 and rsi-e5 build on
it) and **keep** `mini_ork/gates/promotion_gate.py` (imported by
`native_gates`, `gate_registry`, `judge_validity`, `verifier_audit`,
`prompt_directives` and two IDE pages).

All paths are relative to the repository root.

## Mechanism (exact spec)

1. Delete these files:
   - `mini_ork/cli/improve.py`
   - `mini_ork/cli/eval.py`
   - `mini_ork/cli/promote.py`
   - `mini_ork/learning/group_evolver.py`
   - `bin/mini-ork-improve`, `bin/mini-ork-eval`, `bin/mini-ork-promote`
   - `tests/unit/test_mini_ork_improve_py.py`
   - `tests/unit/test_mini_ork_eval_py.py`
   - `tests/unit/test_mini_ork_promote_py.py`
   - `tests/unit/test_group_evolver_py.py`
2. `mini_ork/cli/main.py`:
   - remove the `"improve"`, `"eval"` and `"promote"` entries from
     `_NATIVE_MODULE_SUBS`;
   - remove the three `_HELP` lines that describe them (`improve`,
     `eval --candidate`, `promote --candidate`). Leave every other help line
     byte-for-byte unchanged.
3. `tests/unit/test_native_dispatch_py.py`:
   - remove `"improve", "eval", "promote"` from the `expected` set in
     `test_all_former_exec_subs_registered_natively` (the set must still equal
     `set(_NATIVE_MODULE_SUBS)` exactly);
   - in `test_native_dispatch_uses_python_m` and the test that calls
     `main(["improve", "--help"])` for the PYTHONPATH check, use `"specs"`
     instead of `"improve"` (and `"mini_ork.cli.specs"` in the module
     assertion). Nothing else in that file changes.
4. `docs/FEATURES.md`: delete the three table rows for `mini-ork-improve`,
   `mini-ork-eval` and `mini-ork-promote`.
5. Unknown-subcommand behaviour is unchanged: `mini-ork improve` now takes the
   existing unknown-subcommand path (non-zero exit, help hint). Do not add an
   alias or a deprecation shim.

## Files in scope (touch ONLY these)

- the 11 deleted files listed in step 1
- `mini_ork/cli/main.py`
- `tests/unit/test_native_dispatch_py.py`
- `docs/FEATURES.md`
- `tests/unit/test_eco6_k1_promotion_path_removed.py` (new)

Do NOT modify `mini_ork/learning/benchmark_suite.py`,
`mini_ork/gates/promotion_gate.py`, `mini_ork/cli/apply.py`, any migration, or
any other file. The `workflow_candidates`, `benchmark_results` and
`benchmark_memory` tables stay.

## Tests (`tests/unit/test_eco6_k1_promotion_path_removed.py`, hermetic)

- `importlib.util.find_spec` returns `None` for `mini_ork.cli.improve`,
  `mini_ork.cli.eval`, `mini_ork.cli.promote` and
  `mini_ork.learning.group_evolver`.
- `mini_ork.learning.benchmark_suite` and `mini_ork.gates.promotion_gate`
  still import.
- None of `"improve"`, `"eval"`, `"promote"` is a key of
  `mini_ork.cli.main._NATIVE_MODULE_SUBS` or of `SUBCOMMAND_REGISTRY`, and the
  `_HELP` text has no line starting with `  improve`, `  eval --candidate` or
  `  promote --candidate`.
- `"apply"` is still dispatchable (in `_NATIVE_SUBS` or the registry).
- `bin/mini-ork-improve`, `bin/mini-ork-eval`, `bin/mini-ork-promote` do not
  exist; `bin/mini-ork-recipe-eval` and `bin/mini-ork-self-improve` still do.
- No file under `mini_ork/` contains the string `group_evolver`
  (walk `mini_ork/**/*.py`).

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k1_promotion_path_removed.py tests/unit/test_native_dispatch_py.py tests/unit/test_cli_apply_py.py tests/unit/test_benchmark_suite_py.py tests/unit/test_ide_pages_learn.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k1_promotion_path_removed.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k1-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k1-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test outside the four
  deleted test files passes unchanged.
- `ruff check --select F,E9 mini_ork/cli/main.py` is clean.
- `git grep -n "cli.improve\|cli.eval\b\|cli.promote\|group_evolver" -- mini_ork tests`
  returns nothing.
