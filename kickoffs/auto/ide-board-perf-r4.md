# IDE board perf — revision 4 (Opus review of run ide-board-perf-r3-20261006223927)

## Goal

`board --json --shell` still spends ~3 s in `_runs` because every poll recomputes
the diff counts of finished runs with `git show` (92 subprocesses per poll on the
researcher home). Finished runs never change: count once, keep the numbers.

## Files in scope

- `mini_ork/acp/fleet.py`
- `tests/unit/test_board_cmd.py`

## Fixes (exact)

1. Remove the zero-diff fallback at `fleet.py` ≈ 592-593
   (`if added == 0 and removed == 0 and not (run_dir / CACHE_NAME).is_file(): added, removed = _diff_counts(run_dir)`):
   a done row uses `ts.added, ts.removed` as computed by `task_state`.
2. Fix the comment at ≈ 589-590: `cached_or_computed` (diffs.py:272) is the caller
   that passes `write_cache=False`; `run_diffs` defaults to `write_cache=True`.
3. **Diffstat cache for finished runs.** When a row's state is terminal (done,
   failed, or rolled back — not working/needs_you), persist
   `<run_dir>/diffstat.json` = `{"added": int, "removed": int, "v": 1}`
   (atomic write: temp file + `os.replace`; best-effort, never raises) the first
   time the counts are computed, and read it before any diff computation on later
   polls. Never write it for a working or needs-you run. Do not change
   `acp-diffs.json` semantics in `diffs.py` (it stays run-produced only).
   This must cover both places counts are computed for a row (`task_state` rule 4
   and fleet), so a cached run spawns zero `git` processes.
4. Tests (`tests/unit/test_board_cmd.py`):
   - a published run: first `_runs` writes `diffstat.json`; a second `_runs`
     with `mini_ork.acp.diffs.run_diffs` monkeypatched to raise still returns the
     same counts;
   - a working run never gets `diffstat.json`;
   - extend the frozen `_runs` expectation with one needs_you run and one row with
     a non-empty step, written literally (no loop re-deriving state/mark).

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_board_cmd.py tests/unit/test_ide_pages_*.py`
  passes except `test_ide_pages_changes.py::test_worktrees_lists_run_workspaces_with_merge_and_discard`.
- Timing, run exactly this twice and report both (first fills the cache):
  `cd /tmp && MINI_ORK_ENGINE_ROOT=<this worktree> MINI_ORK_ROOT=<this worktree> <this worktree>/bin/mini-ork board --json --shell --home /Volumes/docker-ssd/Migration/Development/researcher/.mini-ork > /tmp/b.json; echo rc=$?; python3.11 -c "import json;d=json.load(open('/tmp/b.json'));print(len(d['runs']),d['errors'])"`
  with `time`, plus `uptime`. rc must be 0 and the payload valid; a run that fails
  fast is not a timing.
- ruff clean on touched files; diff touches only files in scope.
