# IDE board perf — revision 3 (Opus review of run ide-board-perf-r2-20261006212540)

## Goal

Revision 2 (`wt/ide-pages` HEAD) fixed most items; Opus returned needs_revision for
two blockers and three minors. Fix exactly these; touch nothing else.

## Files in scope

- `mini_ork/acp/fleet.py`
- `mini_ork/ide_pages/header.py` (docstring only)
- `tests/unit/test_board_cmd.py`, `tests/unit/test_ide_pages_actions.py`

## Fixes (exact)

1. **Double diff pass (the poll's dominant cost).** `fleet.py` ≈ lines 571-584:
   `task_state(run_dir, snapshot)` already returns `added`/`removed` for done runs
   (task_state rule 4 calls `_diff_counts`). `fleet_rows` then calls
   `_diff_counts_cached` (misses: no `acp-diffs.json`) and `_diff_counts` again —
   204 `git show` subprocesses per poll on the researcher home. Use
   `ts.added, ts.removed` when `task_state` computed them; only fall back to
   `_diff_counts_cached` / `_diff_counts` when it did not. Fix the false comment
   at ≈ line 578 ("The cache is written by run_diffs on first read" —
   `cached_or_computed` passes `write_cache=False`). Output must stay identical.
   Measure `time bin/mini-ork board --json --shell --home
   /Volumes/docker-ssd/Migration/Development/researcher/.mini-ork` (warm, 3 runs)
   and report the numbers and the machine load (`uptime`).
2. **Worktree-count tests must exercise the fix.** In `tests/unit/test_board_cmd.py`
   replace the two tests that monkeypatch `header._git_common_dir` with: a real
   temp git repo (`git init`, one commit, `git worktree add` ×2), call
   `header.header(home, {})` with `cwd` = the main checkout, and patch
   `subprocess.run` so that only an argv containing `worktree` + `list` raises;
   assert `worktrees == 3`. Add a second case where the home is in a linked
   worktree's main checkout path given as relative `.git` (the regression).
   Do not patch `_git_common_dir`.
3. **`_runs` identity test**: seed ~30 runs (mixed states: done/failed/working,
   some with diffs), and assert against a frozen expected list written literally
   in the test (ids, states, steps, added/removed) — do not derive expected values
   from the function's own output.
4. **Actions test**: derive the subcommand → module map from
   `mini_ork.cli.main._NATIVE_MODULE_SUBS` instead of the hand-copied
   `_SUBCOMMAND_MODULE`.
5. `header.py:24-26` docstring: it now resolves a relative common dir against
   the project; say so.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_board_cmd.py tests/unit/test_ide_pages_*.py tests/unit/test_lane_overlay_resolution.py tests/unit/test_lane_chain_resolution.py`
  passes except `test_ide_pages_changes.py::test_worktrees_lists_run_workspaces_with_merge_and_discard`
  (pre-existing on HEAD; name it if it still fails).
- The `--shell` timing is reported with `uptime`.
- ruff clean on touched files; diff touches only files in scope.
