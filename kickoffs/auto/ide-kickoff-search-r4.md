# IDE board runs search — revision 4 (measured on the researcher home after r3)

WIP commit af07e7d8 (r3). Its Opus review did not run (daily cost cap). I measured it myself on
`/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork` (4,721 runs), deleting
`state/ide-search.sqlite` first, then calling
`bin/mini-ork board runs --query kickoff --limit 50` repeatedly:

```
1  11.33s total=110 indexing=True  {'index': 'indexing: 117/4721 runs'}
2   0.47s total=110 indexing=False {}
…  0.22-0.50s, total stays 110, indexing=False        (index frozen at 117 runs)
— after 32 s —
1   7.72s total=201 indexing=True  {'index': 'indexing: 104/4605 runs'}
2   0.65s total=201 indexing=False {}
```

Three defects (`search.py:386-445`):
1. Before the time budget starts, every candidate — including all ~4,600 never-indexed runs — goes
   through `_stat_mtime_for` plus a per-run `SELECT mtime`. That is the 6-10 s outside the 1.2 s budget.
2. Inside the 30 s window `reindex` returns at once, so a half-built index stops growing, and
   `indexing` reads False while 4,500 runs are still missing — results are silently partial.
3. Even with a correct budget, building 4,721 runs at ~100 per call needs ~45 user queries.

Note for the implementer: the timing gate below is a plain CLI read, not "running the BE or FE" —
it is required; paste the numbers.

## Files in scope

- `mini_ork/ide_pages/search.py`, `mini_ork/cli/board_cmd.py`
- `tests/unit/test_ide_pages_search.py`, `tests/unit/test_board_cmd.py`

## Fixes

1. **No stat for never-indexed runs.** They are stale by definition: candidates = (task_runs ids −
   indexed ids) ordered by `created_at` DESC, then indexed runs with `updated_at > watermark`. Only
   the second group gets `_stat_mtime_for`; read stored mtimes with ONE `SELECT run_id, mtime FROM
   indexed` into a dict (no per-run SELECT).
2. **The budget covers the whole call.** Start the clock at the top of `reindex`; check it inside
   the stat loop and the index loop; total `board runs --query` wall time < 2 s on every call.
3. **The 30 s window applies only to the `updated_at` re-check**, never to never-indexed runs.
   `indexing` + `errors["index"] = "indexing: <indexed>/<total> runs"` are computed on EVERY call
   from two cheap counts (task_runs vs indexed), not only when a scan ran.
4. **Background build.** When never-indexed runs remain after the in-call budget, spawn ONE
   detached indexer (`python -m mini_ork.ide_pages.search --home <home> --build`;
   `start_new_session=True`, stdout/stderr to `<home>/state/ide-search-build.log`) guarded by a
   lock file `<home>/state/ide-search.lock` holding its pid (skip when that pid is alive; remove
   a stale lock). The indexer runs `os.nice(10)`, indexes in batches of 100 with a 0.5 s sleep
   between batches (stay under ram-sentinel's ">50% CPU for 30 s" kill), commits per batch, and
   exits when every run is indexed. Add `"indexing": true` while it runs.
5. Tests: never-indexed runs never reach `_stat_mtime_for` (spy); a call inside the 30 s window with
   unindexed runs still reports `indexing` and still indexes within budget; the budget is enforced
   when the stat loop alone would exceed it (monkeypatch a slow `_stat_mtime_for`); the build spawn
   happens once (lock held) and not at all when fully indexed (spawn monkeypatched); the
   `--build` entrypoint indexes everything in a tmp home in batches.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_board_cmd.py tests/unit/test_ide_pages_search.py tests/unit/test_ide_pages_run.py tests/unit/test_acp_fleet.py` passes — paste the summary line.
- `uvx ruff check` on the touched files is clean — paste it.
- Timing on the researcher home (delete `/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork/state/ide-search.sqlite` first). Paste every line:
  call `MINI_ORK_HOME=/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork bin/mini-ork board runs --query kickoff --limit 50`
  once (< 2 s, `indexing: true`), then every 20 s until `indexing` is gone (each < 2 s; `total`
  grows), then twice more (< 1 s). Paste the build log tail and the total build time.
- Diff touches only files in scope.
