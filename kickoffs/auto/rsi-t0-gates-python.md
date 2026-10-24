# RSI Tier-0 #6 (loop defect D2): the recursive-self-improve gates check Python, not only shell

## Goal

The deterministic gates of `recipes/recursive-self-improve/` are near-vacuous on
the Python runtime. (The bash runtime was removed in 2026-07, and nearly every
patch is now Python.)

- **`verifiers/no-regression.py`:**
  - The syntax arm diffs only `*.sh bin/*` and runs `bash -n` on them. A
    Python syntax error in a changed `.py` file passes.
  - Today `bin/mini-ork` is a Python file, so a change to it would be fed to
    `bash -n` and false-fail.
- **`verifiers/self-tests-pass.py`:**
  - Its test subprocess has no timeout, so a hung suite hangs the iteration.
  - The failure count adds `_count("failed") + _count("error") + _count("errors")`,
    and the `(\d+) error` pattern also matches inside "2 errors". So errors are
    counted twice in the evidence. (This is evidence only; the verdict is
    decided by `rc`.)

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `recipes/recursive-self-improve/verifiers/no-regression.py`

- **Python syntax arm.** List changed Python files as the union, in this order,
  de-duplicated, of:
  - `git -C $WT diff --name-only HEAD -- '*.py'`;
  - `git -C $WT ls-files --others --exclude-standard -- '*.py'` (new,
    untracked files).
- **Python-shebang `bin/` files.** Add any `bin/*` file from the existing
  shell-arm list whose first line is a shebang naming python (`python`,
  `python3`, or `env python…`).
- **The check.** For each file that exists, read it as UTF-8 (errors=replace)
  and call `compile(source, path, "exec")`. `SyntaxError` or `ValueError` →
  append the repo-relative path to a new list `py_syntax_failures`. No `.pyc`
  is written.
- **Shell arm.** A `bin/*` file goes to `bash -n` **only** when its first line is
  a shebang naming a shell (`sh`, `bash`, `zsh`, `dash`, or `env` + one of
  those). `*.sh` files are unchanged.
- **Evidence line** after the existing one:
  `changed_py_files=<N> py_syntax_failures=<M>`.
- **Verdict.** `passed = 0` when `py_syntax_failures` is non-empty.
- **Result JSON.** Gains `"py_syntax_failures": [...]`. Every existing key and
  the sidecar file are unchanged.

### 2. `recipes/recursive-self-improve/verifiers/self-tests-pass.py`

- **Timeout.** Pass `timeout=<T>` to the test `subprocess.run`, where `T` is
  `int(os.environ.get("MINI_ORK_SELF_IMPROVE_TEST_TIMEOUT_S", "2400"))`. A
  non-integer or non-positive value fails closed: print an error and emit
  `pass: false` with `"reason": "bad MINI_ORK_SELF_IMPROVE_TEST_TIMEOUT_S"`.
- **On `subprocess.TimeoutExpired`:**
  - write `tests timed out after <T>s` to the evidence;
  - emit the usual result with `"pass": false`, `"pytest_rc": null`,
    `"timed_out": true`.
- **Every other result** gains `"timed_out": false`.
- **Error count.** Count `n_failed` as `_count("failed")` plus one match of
  `(\d+) errors?\b`, so an "errors" count is counted once. The `rc` rules that
  decide `passed` are unchanged.

## Files in scope (touch ONLY these)

- `recipes/recursive-self-improve/verifiers/no-regression.py`
- `recipes/recursive-self-improve/verifiers/self-tests-pass.py`
- `tests/unit/test_rsi_gates_python.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_rsi_gates_python.py`: subprocess against the real scripts, temp git repos, no network)

### The `no-regression.py` setup

For each case, create a temp git repo (`git init`, set user name/email, one
committed `README.md`). Then run `python3 recipes/recursive-self-improve/verifiers/no-regression.py`
with:

- `MINI_ORK_RUN_DIR=<tmp run dir>`;
- `MINI_ORK_SELF_IMPROVE_WORKTREE=<repo>`;
- `MINI_ORK_DB=<nonexistent path>`.

Parse the JSON on its stdout.

### `no-regression.py` cases

- **A Python syntax error fails.** An untracked `pkg/bad.py` containing
  `def f(:\n` → `"pass": false` and `"pkg/bad.py" in py_syntax_failures`.
- **Valid Python passes.** An untracked `pkg/ok.py` containing `x = 1\n` →
  `"pass": true` and `py_syntax_failures == []`.
- **A modified tracked file is checked.** Commit `pkg/m.py` (`x = 1`), then
  modify it to `x = (\n` → `"pass": false`.
- **A Python-shebang `bin/` file skips `bash -n`.** Commit `bin/tool` holding
  `#!/usr/bin/env python3\nprint("hi")\n`, then modify it to print `"ho"` →
  `"pass": true` and `syntax_failures == []`.
- **A shell-shebang `bin/` file still gets `bash -n`.** Commit `bin/run` holding
  `#!/bin/bash\necho ok\n`, then modify it to `#!/bin/bash\nif then\n` →
  `"pass": false` and `"bin/run" in syntax_failures`.
- **The sidecar is unchanged.** The run dir holds
  `verifier-result-no-regression.json` with the same `pass` value as stdout.

### `self-tests-pass.py` cases

Read the script's env inputs first and set the minimum it needs. A test command
that sleeps longer than `MINI_ORK_SELF_IMPROVE_TEST_TIMEOUT_S=2` →
`"pass": false` and `"timed_out": true`.

If the script cannot be driven hermetically with a custom test command, test the
pure pieces instead: extract the timeout parse and the count into small
module-level functions and import them.

## Verification commands

- `pytest -q tests/unit/test_rsi_gates_python.py tests/unit/test_rsi_workflow_shape_gate.py`
- `python3 recipes/recursive-self-improve/verifiers/tests/test_no_regression_bench.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_rsi_gates_python.py --junitxml="${MINI_ORK_RUN_DIR}/rsi-gates-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rsi-gates-junit.xml`

## Success criteria

- Both verification commands pass, and the existing bench script is unchanged.
- `ruff check --select F,E9 recipes/recursive-self-improve/verifiers/no-regression.py recipes/recursive-self-improve/verifiers/self-tests-pass.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
