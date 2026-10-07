# IDE kickoff tab + board runs search — revision 2 (Opus review of run ide-kickoff-search-20261007091357)

The Kickoff tab (spec.markdown, run.py tab after DAG, 200,000-char cap) passed review — do not touch it.
Fix only the `board runs` paging/search path below.

## Files in scope

- `mini_ork/acp/fleet.py`, `mini_ork/acp/history.py`
- `mini_ork/cli/board_cmd.py`, `mini_ork/ide_pages/search.py`
- `tests/unit/test_board_cmd.py`, `tests/unit/test_ide_pages_search.py`

## Fixes (exact)

1. **Blocker — real rows for every run id, not stubs.** `fleet_rows` clamps to `MAX_LIMIT = 50`
   (`fleet.py:58`), so `_rows_for` (`board_cmd.py:57-100`) emits `_minimal` stubs
   (`title ""`, `recipe ""`, `state "working"`) for every run past the newest 50.
   - In `fleet.py`, extract steps 4-5 of `fleet_rows` (the `_events_by_run` batch +
     `task_state` per row + `FleetRow` build, lines ~466-524) into a public
     `rows_from_candidates(home, candidates: list[dict]) -> list[FleetRow]`, where
     `candidates` are `list_runs`-shaped dicts. `fleet_rows` calls it for `shown`;
     behaviour of `fleet_rows` is unchanged.
   - In `history.py`, add `runs_by_ids(home, run_ids: list[str]) -> list[dict]` returning
     `list_runs`-shaped dicts (same keys, same title rule, same safe-token filter) for exactly
     those ids, in input order, skipping ids not in `task_runs`. One `SELECT … WHERE id IN (…)`.
   - `_rows_for(home, run_ids)` becomes: `runs_by_ids` → `rows_from_candidates` → row dicts.
     Delete `_minimal` and the "200-candidate window" docstring claim.
   - No-query paging: use the `list_runs(limit, offset)` rows directly with
     `rows_from_candidates` (no second lookup).
   - `_runs` (the hot `board --json` / `--shell` path): build row dicts from the `FleetRow`s
     `fleet_rows` already returned — call `fleet_rows` exactly once per poll.
2. **Blocker — surface degraded mode.** Pass the verb's `errors` dict into
   `_search.search(..., errors=errors)` and `_search.reindex(home, errors=errors)` so
   "FTS5 unavailable, fell back to LIKE scan" reaches the payload's `errors`.
3. **Drop the wasted first search** (`board_cmd.py:538-551`): reindex, then search once.
4. **Reindex staleness from `stat()` only** (`search.py:227`): compute each run's max mtime
   from `stat()` of the run dir, the kickoff path and the artifact files — do not read
   bodies. Read bodies (`_body_for`) only for the ≤ `max_runs` selected candidates.
5. **FTS5 probe** (`search.py:~81`): probe with `sqlite3.connect(":memory:")`, not a
   `CREATE VIRTUAL TABLE _fts5_probe` in the shared index file. Cache the result per process.
6. **Searchable by run id**: append the run id to `body` at index time (it is UNINDEXED today).
7. **LIKE fallback honours `offset`** (`search` docstring says it ignores it): page the
   LIKE matches with the same `limit`/`offset` and return the true match count as `total`.
8. Fix the `_like_term` docstring (it strips `"()`, not `%`/`_`).

## Tests (each must fail on the WIP commit 7d4ff20f)

- Seed 60 published runs with distinct titles/recipes and terminal status `done`;
  `board runs --offset 48 --limit 4` returns 4 rows with the right title, recipe and
  `state == "done"` for the rows past index 50; `total == 60`, `has_more` true; offset 56 → 4
  rows, `has_more` false.
- `board runs --query <word only in the oldest run's kickoff>` returns that run with its real
  title/recipe/state.
- Search-path paging: ≥ 12 matches, `--limit 5 --offset 0/5/10` → disjoint pages, correct
  `has_more` on each, `total` = match count.
- `board runs --query <run id>` finds a run whose kickoff does not contain its id.
- With FTS5 forced unavailable (monkeypatch the probe), the payload `errors` contains the
  fallback note and `--offset` pages the LIKE results.
- `_runs` calls `fleet_rows` once (spy).
- Reindex on a warm index does not open any body file (spy `_body_for` → 0 calls when
  nothing is stale).

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_board_cmd.py tests/unit/test_ide_pages_search.py tests/unit/test_ide_pages_run.py tests/unit/test_acp_fleet.py` passes — paste the summary line.
- `ruff check` on the touched files is clean.
- Timing on the large real home (paste both numbers):
  `MINI_ORK_HOME=/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork bin/mini-ork board runs --query kickoff --limit 50`
  run twice (cold, then warm). Warm must be < 2 s.
- Diff touches only files in scope.
