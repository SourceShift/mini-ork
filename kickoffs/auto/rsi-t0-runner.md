# RSI Tier-0 #8 (loop defects D4 + D5): the self-improve runner arms `--deadline` and closes the learning_record lifecycle

## Goal

Two defects in `mini_ork/cli/self_improve.py`, both found by the loop itself
(2026-10-23 cycle-3 synthesis).

**D5: no `--deadline`.**
- The nested `_default_dispatch` bounds an iteration only with coreutils
  `timeout <per_iter>`, so the inner run never knows its time budget.
- As a result, the run's per-node deadline caps (`_cap_dispatch_timeout`, the
  reflect cap in `mini_ork/cli/main.py`) are never armed. Nodes keep their
  1200–1500 s timeouts against a ~2160 s iteration.
- An overrun is killed with rc 124, which records no learning rows at all.
  Cycle 3 lost its $7.35 this way.

**D4: `learning_record` never closes a finding.** On a successful iteration:
- `record_success` runs **before** `promote_synthesis_findings`.
- `promote_synthesis_findings` inserts every ranked row as `open`.
- Nothing ever writes `deferred`, so `record_success`'s supersede sweep is dead.
- The implemented rank-1 finding stays `open` forever (shipped finding id 2,
  commit c1632634, is still `open`). So dedupe and convergence detection cannot
  work.

**Plus a wording fix for D3.** The kickoff template writes the branch name in
backticks, so the scope parser reads it as a path.

All paths are relative to the repository root.

## Mechanism (exact spec)

All in `mini_ork/cli/self_improve.py`.

### 1. D5: the dispatch argv

- Add a module-level function
  `build_dispatch_argv(root: str, kickoff: str, timeout_s: int) -> list[str]`
  returning:

  ```
  ["timeout", str(timeout_s), os.path.join(root, "bin", "mini-ork"),
   "run", "--deadline", str(max(60, int(timeout_s) - 120)),
   "recursive-self-improve", kickoff]
  ```

  The run's own deadline fires 120 s before the coreutils backstop, so the run
  can finish its publish and learning steps. The floor is 60 s.
- `_default_dispatch` builds its argv with `build_dispatch_argv(root, kickoff, timeout_s)`.
  Everything else it does is unchanged: `cwd`, stdout/stderr to the log, and
  `env`.

### 2. D4: the success branch of `_iter_run`

The block that currently calls `record_success(...)` then
`promote_synthesis_findings(...)` becomes, in this order:

1. `promote_synthesis_findings(db, run_id, it, <synthesis.md path>)` (unchanged
   function).
2. A new function
   `close_iteration_findings(db, run_id, iter_) -> tuple[int, int]`:
   - **Resolve rank 1.** `UPDATE learning_record SET outcome='resolved', updated_at=<now> WHERE run_id=? AND iter=? AND rank=1 AND outcome='open'`.
   - **Defer the rest.** `UPDATE learning_record SET outcome='deferred', updated_at=<now> WHERE run_id=? AND iter=? AND rank>1 AND outcome='open'`.
   - Returns `(resolved_count, deferred_count)`.
   - Fail-open: any `sqlite3.Error` returns `(0, 0)`.
3. `record_success(db, run_id, it, branch, sha)`. Its supersede sweep must skip
   the current iteration's rows:

   ```
   UPDATE learning_record SET outcome='superseded', updated_at=?
    WHERE outcome='deferred' AND NOT (run_id=? AND iter=?)
   ```

   The rest of `record_success` is unchanged.

### 3. D3 wording

In the kickoff template text (the `## Scope` bullet "an isolated git worktree
on branch `{branch}`"), remove the backticks around `{branch}`. The text
becomes `on branch {branch}.`. Change nothing else in the template.

## Files in scope (touch ONLY these)

- `mini_ork/cli/self_improve.py`
- `tests/unit/test_self_improve_runner_t0.py` (new)

Do NOT modify any other file. Every test in
`tests/unit/test_mini_ork_self_improve_py.py` must pass unchanged, including
`test_record_success`. A deferred row from a *different* run is still
superseded.

## Tests (`tests/unit/test_self_improve_runner_t0.py`: hermetic temp sqlite, no subprocess runs)

Build the DB the way `tests/unit/test_mini_ork_self_improve_py.py:_fresh_db`
does (import it, or apply `db/migrations/0017_self_improve_learning.sql`).

- **The argv.** `build_dispatch_argv("/r", "/k.md", 2160)` equals
  `["timeout", "2160", "/r/bin/mini-ork", "run", "--deadline", "2040", "recursive-self-improve", "/k.md"]`.
- **The 60 s floor.** `build_dispatch_argv("/r", "/k.md", 100)` has `"60"` right
  after `"--deadline"`.
- **Resolve and defer.** Insert three `open` rows for (`run-1`, iter 2) with
  ranks 1, 2, 3, plus one `open` row for (`run-0`, iter 1, rank 1). Then
  `close_iteration_findings(db, "run-1", 2)`:
  - returns `(1, 2)`;
  - leaves rank 1 `resolved` and ranks 2 and 3 `deferred`;
  - leaves the `run-0` row `open`.
- **The current iteration is not superseded.** After that,
  `record_success(db, "run-1", 2, "self-improve/iter-2", "abc12345")` leaves the
  two `deferred` rows of (`run-1`, iter 2) `deferred`.
- **A later iteration supersedes them.** A later
  `record_success(db, "run-2", 3, "self-improve/iter-3", "def67890")` turns
  them `superseded`.
- **Fail-open.** `close_iteration_findings` on a path that is not a database
  returns `(0, 0)`.
- **The template wording.** The rendered kickoff template no longer contains
  `` `self-improve/ `` (no backticked branch). Find the template builder in
  `self_improve.py`, call it with a sample branch, and assert
  `"on branch self-improve/iter-9-x"` is in the text.

## Verification commands

- `pytest -q tests/unit/test_self_improve_runner_t0.py tests/unit/test_mini_ork_self_improve_py.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_self_improve_runner_t0.py --junitxml="${MINI_ORK_RUN_DIR}/rsi-runner-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/rsi-runner-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/cli/self_improve.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
