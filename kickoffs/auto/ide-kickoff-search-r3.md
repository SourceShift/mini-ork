# IDE board runs search — revision 3 (Opus review of run ide-kickoff-search-r2-20261007095002)

WIP commit 1640813c passed every functional fix (real rows past 50, `fleet_rows` once, errors
threaded, run id searchable, LIKE paging, `:memory:` FTS5 probe). It fails the speed gate: a warm
`board runs --query kickoff --limit 50` on the researcher home (4,788 runs) takes 5.3-5.5 s
(gate < 2 s). Opus profiled one warm call (9.6 s): `reindex` 7.8 s, of which
`_stat_mtime_for` (`search.py:286`) = 7.4 s = 70,562 `stat` calls (~15 per run, every query);
`_search_fts` 1.5 s. Cold start needs ~12 calls because reindex takes only 400 runs per call.

## Files in scope

- `mini_ork/ide_pages/search.py`, `mini_ork/cli/board_cmd.py`
- `tests/unit/test_ide_pages_search.py`, `tests/unit/test_board_cmd.py`

## Fixes (exact)

1. **No full staleness scan per query.** Keep a `meta(key PRIMARY KEY, value)` table in
   `ide-search.sqlite` with `last_scan_at` (epoch) and `watermark` (max `task_runs.updated_at`
   seen). On each query:
   - If `now - last_scan_at < 30` s → skip the scan entirely.
   - Otherwise candidates = runs not in `indexed` + runs with `task_runs.updated_at > watermark`
     (one SQL query against state.db; no per-run `stat`). Stat-based staleness only for those
     candidates. Then update `last_scan_at` / `watermark`.
2. **Index by time budget, not count.** Replace the 400-run cap with a 1.2 s wall-clock budget
   per call (newest first). When runs remain unindexed, set `errors["index"] =
   "indexing: <done>/<total> runs"` and add `"indexing": true` to the payload so the IDE can say
   the results are partial.
3. **Cheaper staleness when it is needed:** one `os.scandir(run_dir)` per run using
   `DirEntry.stat()` (no `is_file()`+`stat()` pairs, no repeated globs).
4. **Faster FTS query.** Store the run's mtime as an UNINDEXED column in `runs_fts` and order by
   `rank` (FTS5's built-in bm25) — no `LEFT JOIN indexed`. Cap each run's indexed body at 64 KB
   (kickoff first, then the artifacts in their current order) to shrink the 95 MB index. Count
   with the plain `SELECT count(*) … MATCH ?`.
5. **No-query paging** (`board_cmd.py:560-567`): pass the `list_runs` rows straight to
   `rows_from_candidates`; do not drop them to ids and re-read via `runs_by_ids`.
6. Fix the stale `_runs_verb` docstring (`board_cmd.py:533-534`, mentions the deleted
   `_minimal` / "200-candidate window").
7. Tests: `test_board_cmd.py:1092` asserts exact ids/titles for offset 48 (newest-first →
   `rids[11]..rids[8]`); fix the reversed ordering comments at `:1041` and `:1074`
   (`rids[0]` is the oldest); `:1204` LIKE paging test seeds ≥ 2 matches and checks disjoint
   pages + `total`. New tests: a second query within 30 s does no staleness scan (spy
   `_stat_mtime_for` → 0 calls); a run whose `updated_at` moves past the watermark is reindexed;
   the time budget stops early and reports `indexing`.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_board_cmd.py tests/unit/test_ide_pages_search.py tests/unit/test_ide_pages_run.py tests/unit/test_acp_fleet.py` passes — paste the summary line.
- `ruff check` on the touched files is clean.
- Timing on the researcher home (the existing index there is a derived cache; delete
  `/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork/state/ide-search.sqlite` first so
  the cold path is measured). Paste every wall time:
  `MINI_ORK_HOME=/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork bin/mini-ork board runs --query kickoff --limit 50`
  run repeatedly until `indexing` is gone, then twice more. Each call < 2 s; the last two (warm) < 1 s.
- Diff touches only files in scope.
