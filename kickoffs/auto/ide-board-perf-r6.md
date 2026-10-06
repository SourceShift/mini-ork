# IDE board perf — revision 6 (Opus review of run ide-board-perf-r5-20261007003516)

## Files in scope

- `mini_ork/acp/task_state.py`, `tests/unit/test_board_cmd.py`

## Fixes (exact)

1. **Cache only real results.** `run_diffs` never raises; it returns `[]` when the
   summary is missing, the worktree is gone or `files_changed` is empty. So in
   `task_state.py` ≈ 367-370 write `diffstat.json` only when the counts came from
   the run's own `acp-diffs.json` (`cached_or_computed` returned `from_cache=True`)
   or the computed diff list is non-empty. Have `_diff_counts` return
   `(added, removed, cacheable: bool)` (or equivalent) — `cacheable` is False on an
   exception or an empty computed list. An empty result is recomputed next poll
   (cheap: `run_diffs` returns before calling git).
2. **Fix `test_diffstat_not_written_when_compute_fails`** (`test_board_cmd.py` ≈ 706-731):
   do NOT seed `acp-diffs.json` before poll 1 (with `run_diffs` patched to raise →
   assert no `diffstat.json`); seed it only before poll 2 → real counts and the file
   is written. Remove the leftover unlink and the wrong comment at ≈ 722-731.
   Add: poll with `run_diffs` returning `[]` and no `acp-diffs.json` → no
   `diffstat.json` written.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_board_cmd.py tests/unit/test_acp_task_state.py tests/unit/test_ide_pages_*.py`
  passes except `test_ide_pages_changes.py::test_worktrees_lists_run_workspaces_with_merge_and_discard`
  — run it and paste the summary line.
- Diff touches only files in scope.
