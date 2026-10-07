# IDE board runs search — revision 5: simplify staleness (Opus review of ide-kickoff-search-r4-20261007111217)

WIP commit 519b013f. Opus found that the 30 s window plus the global `watermark` are the source of
most remaining bugs: the window still blocks never-indexed runs, the stat-loop budget is not
enforced, and advancing a global watermark while the backlog builds loses edited runs forever. This
revision REMOVES both and uses a per-run record instead. Keep everything else (rows, paging, FTS
query, 64 KB bodies, `--build` entrypoint) unless listed.

## Files in scope

- `mini_ork/ide_pages/search.py`, `mini_ork/cli/board_cmd.py`
- `tests/unit/test_ide_pages_search.py`, `tests/unit/test_board_cmd.py`

## Design (exact)

1. **Per-run staleness, no stat, no window, no global watermark.** The `indexed` table stores, per
   run, the `task_runs.updated_at` value seen when it was indexed (`seen_updated_at`; migrate by
   dropping and rebuilding the derived index when the column is missing). Each call reads
   `task_runs (id, created_at, updated_at, kickoff_path)` once and `indexed (run_id,
   seen_updated_at)` once into dicts. Work list = never-indexed runs (newest `created_at` first),
   then indexed runs whose `updated_at != seen_updated_at` (newest first). No `_stat_mtime_for` in
   the per-query path. Delete `_SCAN_WINDOW_S`, `last_scan_at`, `watermark` and their meta keys.
2. **One budget for the whole call** (start the clock at the top of `reindex`), checked BEFORE each
   run is indexed; at least one run is indexed per call when work remains.
3. **Progress is computed, not remembered:** after the work, `remaining` = never-indexed + still
   stale; `indexing` = `remaining > 0`, `errors["index"] = "indexing: <indexed>/<total> runs"`
   where `<indexed>` = runs whose `seen_updated_at` equals their current `updated_at`.
4. **Builder** only when `remaining > 0` AFTER the in-call work. Claim
   `<home>/state/ide-search.lock` with `os.open(…, O_CREAT | O_EXCL | O_WRONLY)` BEFORE spawning;
   write the child's pid into it; when the lock exists and its pid is dead, remove it and try the
   claim once more; when it is alive, do nothing. `_run_build` re-checks it owns the lock (pid ==
   os.getpid() — have the parent pass the lock path and let the child write its own pid after
   claiming, or write the pid right after Popen and have the child verify), indexes with the same
   work list in batches of `_BUILD_BATCH` (100) with `_BUILD_SLEEP_S` (0.5 s) between, and removes
   the lock in a `finally`. No extra `COUNT(*)` on state.db — reuse the rows already read.

## Tests

- An autouse fixture in both test files monkeypatches `search._spawn_indexer` to a recorder (no real
  builder ever runs in unit tests).
- Fix `test_reindex_skips_scan_within_30s_window` (delete it: the window is gone) and
  `test_ide_pages_search.py:455` (`assert n == 0`) — replace with: 5 new runs on a warm home are
  indexed by the next call within budget.
- Edited run during a backlog: 2 indexed runs, edit run[0]'s kickoff to contain `zebrafish` and bump
  its `updated_at`, seed 30 new runs, `reindex(time_budget=1e-9)` ×6, then `reindex()` until
  `remaining == 0` → `search('zebrafish')` returns run[0].
- Budget: 40 never-indexed runs with a `_body_for` monkeypatched to sleep 0.05 s,
  `time_budget=0.12` → 1-3 runs indexed, elapsed < 0.3 s, `indexing` true.
- Lock: two `_maybe_spawn_builder` calls in a row → exactly one spawn; a lock holding a dead pid →
  replaced and one spawn; a live pid → no spawn.
- Builder batching: 7 runs, `monkeypatch.setattr(search, "_BUILD_BATCH", 3)` and
  `_BUILD_SLEEP_S = 0` via monkeypatch → 3 commits (spy), all 7 indexed, lock removed.
- `board runs` tests unchanged in meaning; `has_more` / paging as before.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_board_cmd.py tests/unit/test_ide_pages_search.py tests/unit/test_ide_pages_run.py tests/unit/test_acp_fleet.py` → 0 failed. Paste the line.
- `uvx ruff check` on the touched files → clean. Paste it.
- **Required timing (this is a CLI read, not running the BE/FE — do not skip it).** Delete
  `/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork/state/ide-search.sqlite*` and the
  lock, then run
  `MINI_ORK_HOME=/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork PYTHONPATH=$PWD python3.11 -m mini_ork.cli.main board runs --query kickoff --limit 50`
  once, then every 20 s until `indexing` is false, then twice more. Paste every wall time and
  `total`, the builder log tail and total build time. Each call < 2 s; the last two < 1 s.
  (`bin/mini-ork` runs the MAIN engine, not this worktree — use the PYTHONPATH form above.)
- Diff touches only files in scope.
