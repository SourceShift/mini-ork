# IDE board perf — revision 5 (Opus review of run ide-board-perf-r4-20261006230254)

## Files in scope

- `mini_ork/acp/task_state.py`, `mini_ork/acp/fleet.py`, `tests/unit/test_board_cmd.py`

## Fixes (exact)

1. **Never cache a failure.** `task_state.py` ≈ 343-344 writes `diffstat.json`
   from `_diff_counts`, which turns any exception into `(0, 0)`. Make the count path
   report success separately (e.g. a helper returning `None` on exception) and
   write `diffstat.json` only after a successful computation. A failed computation
   shows `(0, 0)` for that poll only and is retried next poll.
2. **Test the right cache.** `test_board_cmd.py` ≈ 682-694: delete
   `home/runs/<id>/acp-diffs.json` before the second `_runs` call, so only
   `diffstat.json` can answer 4/2 while `run_diffs` raises. Add a test: poll 1 with
   `run_diffs` raising → no `diffstat.json` written; poll 2 with real diffs → real
   counts and the file is written.
3. Remove dead `_diff_counts` / `_diff_counts_cached` from `fleet.py` (update the
   test that calls `_diff_counts_cached`), or keep one with a comment naming its caller.
4. Comment at `fleet.py` ≈ 590: say "`run_diffs` defaults to `write_cache=True`;
   `cached_or_computed` (diffs.py:272) passes `write_cache=False`".
5. Atomic write with a per-writer temp file (`tempfile.mkstemp(dir=run_dir)` then
   `os.replace`).

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_board_cmd.py tests/unit/test_ide_pages_*.py`
  passes except `test_ide_pages_changes.py::test_worktrees_lists_run_workspaces_with_merge_and_discard`.
- Paste into the implementer summary the literal output of, run twice:
  `cd /tmp && uptime && time (MINI_ORK_ENGINE_ROOT=<this worktree> MINI_ORK_ROOT=<this worktree> /Volumes/docker-ssd/ps/mini-ork/.venv/bin/python <this worktree>/bin/mini-ork board --json --shell --home /Volumes/docker-ssd/Migration/Development/researcher/.mini-ork > /tmp/b.json; echo rc=$?) && /Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -c "import json;d=json.load(open('/tmp/b.json'));print(len(d['runs']),d['errors'])"`
- ruff clean on touched files; diff touches only files in scope.
