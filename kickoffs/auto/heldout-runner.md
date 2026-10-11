# Held-out task runner: execute mined tasks and grade them with hidden tests

## Goal

`/Volumes/docker-ssd/ps/mini-ork-worktrees/heldout-runner/evals/heldout/mined/manifest.json`
holds 92 SWE-bench-style tasks mined from this repo's own `fix:` commits
(produced by `scripts/mine_heldout_tasks.py`). Nothing executes them yet.
Build ONE new script that runs a solver on each selected task and grades the
result with that task's own hidden tests, writing a results file that
`scripts/heldout_eval.py` can score.

## Files in scope

- `/Volumes/docker-ssd/ps/mini-ork-worktrees/heldout-runner/scripts/run_heldout.py` — NEW, the runner.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/heldout-runner/tests/unit/test_run_heldout_py.py` — NEW, its unit tests.

Read-only references (do NOT edit):
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/heldout-runner/scripts/mine_heldout_tasks.py` — task schema, `run_tests()` junit parsing to reuse by import.
- `/Volumes/docker-ssd/ps/mini-ork-worktrees/heldout-runner/scripts/heldout_eval.py` — the results format it consumes: `{task_id: {"passed": bool, "cost_usd": float}}`.

No other file may change.

## Task schema (each manifest entry)

`id`, `fix_sha`, `base_sha`, `problem_statement`, `src_files`, `test_files`,
`fail_to_pass` (list of junit ids `classname::name`), `pass_to_pass`,
`split` (`dev`|`test`), `difficulty` (`easy`|`medium`|`hard`), `weak_signal` (bool).

## Required behaviour of scripts/run_heldout.py

1. CLI flags: `--manifest` (default the path above, repo-relative), `--split`
   (default `dev`; refuse `test` unless `--allow-test-split` is also given),
   `--difficulty` (optional filter), `--exclude-weak`, `--limit N`,
   `--recipe` (default `code-fix`), `--solver-cmd` (optional override, see 4),
   `--out` (results JSON path), `--timeout` (seconds per task, default 3600),
   `--dry-run` (print selected task ids, run nothing).
2. Per task, materialise the BASE in a scratch directory with
   `git worktree add --detach <tmp> <base_sha>` (never create a branch), and
   always remove it afterwards (`git worktree remove --force`) even on error.
3. Write a kickoff markdown into the scratch dir from `problem_statement`, with a
   "Files in scope" section listing `src_files` as absolute scratch paths, and a
   Definition of Done saying the behaviour described must be fixed. The kickoff
   must NOT include the contents or names of `test_files` (tests are hidden).
4. Solve: by default run
   `<repo>/bin/mini-ork run --json <recipe> <kickoff>` with env
   `MO_TARGET_CWD=<scratch>`, `MO_ALLOW_FRAMEWORK_CWD=1`,
   `MINI_ORK_RUN_ID=heldout-<task id>-<unix ts>`, bounded by `--timeout`.
   Parse `cost_usd` from the `--json` result line when present, else 0.0.
   With `--solver-cmd`, run that shell command instead with cwd=<scratch> and
   env `TASK_ID`, `KICKOFF` (path), `SCRATCH` set — this is what the unit tests use.
5. Grade (a pure function `grade(scratch, task, python, timeout) -> dict`): overwrite
   every path in `test_files` with its content at `fix_sha`
   (`git -C <scratch> checkout <fix_sha> -- <test_files>`), then run them with
   `mine_heldout_tasks.run_tests`. `passed` is True iff EVERY id in
   `fail_to_pass` and `pass_to_pass` has outcome `passed`. Record the per-id
   outcomes that were not `passed`.
6. Results: write `{task_id: {"passed", "cost_usd", "difficulty", "weak_signal",
   "failed_ids", "solver_rc", "seconds"}}` to `--out`, checkpointed after every
   task, and resumable (skip task ids already present in `--out`). Print a one-line
   summary (resolved / attempted, total cost) to stderr at the end.

## Tests (tests/unit/test_run_heldout_py.py)

Against a real temp git repo (pattern: `tests/unit/test_mine_heldout_tasks_py.py`)
with a base commit containing a bug and a fix commit that fixes it and adds a test:
- a `--solver-cmd` that applies the gold source change (e.g. `git checkout <fix_sha> -- <src file>`) → task `passed: True`;
- a no-op `--solver-cmd` → `passed: False` with the fail_to_pass id in `failed_ids`;
- a solver that edits the TEST file to force green still grades False, because
  grading restores tests from `fix_sha`;
- the kickoff written for the solver does not mention any `test_files` path;
- `--split test` without `--allow-test-split` exits non-zero;
- a second invocation with the same `--out` re-runs nothing (resume);
- the scratch worktree is gone after each task (`git worktree list`).

## Definition of Done

- Both files exist; `python -m pytest -q tests/unit/test_run_heldout_py.py` passes.
- `ruff check scripts/run_heldout.py tests/unit/test_run_heldout_py.py` is clean.
- `python scripts/run_heldout.py --dry-run --split dev --limit 3` prints 3 task ids.
- No LLM is called by the unit tests.
