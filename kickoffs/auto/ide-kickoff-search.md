# IDE: the run's Kickoff tab, and Threads search + paging (`board runs`)

## Goal

Two IDE needs, both read-only data:
1. A run tab must show the full kickoff `.md` the run was given — its own **Kickoff**
   tab, right after DAG — not the one-line description in the header.
2. The Threads board needs full-text search over runs by feature name (title, kickoff
   text, main artifacts) and paging past the first 50 runs.

## Files in scope

- `mini_ork/ide_pages/spec.py` — new `markdown(title, text, path=None, **opt)` section helper
- `mini_ork/ide_pages/run.py` — new `kickoff` tab
- `mini_ork/ide_pages/search.py` — new: the run search index + query
- `mini_ork/cli/board_cmd.py` — new verb `runs`
- `tests/unit/test_ide_pages_run.py`, `tests/unit/test_ide_pages_search.py` (new), `tests/unit/test_board_cmd.py`

No other file changes.

## 1. Kickoff tab (exact)

- `spec.markdown(title, text, path=None, **opt)` → section
  `{"type": "markdown", "title", "text", "path"}` plus the usual `note/actions/full/col`.
  Add `markdown` to the section list in the module docstring.
- `run.py`: tab `("kickoff", "Kickoff")` inserted right after `("dag", "DAG")`. Content:
  one full-width `markdown` section titled with the kickoff file name, `text` = the
  whole kickoff file (cap 200,000 chars; say "(truncated)" at the end when capped),
  `path` = its absolute path (from `task_runs.kickoff_path`, else
  `<home>/runs-inbox/<run>.md`, else the run dir's `kickoff*.md`; reuse
  `board_cmd._kickoff`'s lookup). No kickoff found → a `list` section with one
  `dot("No kickoff file recorded for this run")`.

## 2. `board runs` (exact)

`mini-ork board runs [--query Q] [--offset N] [--limit M] [--json] [--home H]` →
`{"ok": true, "runs": [row…], "total": int, "offset": N, "has_more": bool}`; rows have
exactly the shape of `board --json`'s `runs` (reuse `board_cmd._runs`' row builder on
the selected run ids — factor a helper if needed). Default limit 50, max 200.

- **No query**: newest first, paging over all runs (`offset`/`limit`) — not limited to
  the first 200 candidates; `total` = all runs.
- **With a query**: full-text search over each run's id, title, recipe, kickoff text,
  and its main artifacts in the run dir (`implementer-summary.json`, `verdict.json`,
  and top-level `*.md` reports such as `lens-*.md`, `synthesis.md`, `cycle-report.md`;
  ≤ 200 KB each). Index: SQLite FTS5 in `<home>/state/ide-search.sqlite` (never
  `state.db`): `runs_fts(run_id UNINDEXED, title, recipe, body)` + `indexed(run_id
  PRIMARY KEY, mtime REAL)`. On each query, (re)index runs whose run dir or kickoff
  mtime is newer than recorded — newest first, at most 400 per call so a query stays
  fast; the rest catch up on later calls. Query terms are AND-ed prefix matches
  (`term*`), escaped for FTS syntax; rank by bm25 then recency. `total` = match count.
  If FTS5 is unavailable, fall back to a LIKE scan over title/recipe/kickoff (say so
  in `errors`).
- Read-only toward mini-ork state (the search index is the only file written).

## Tests

- run page: `kickoff` tab exists after `dag`, returns a full-width markdown section with
  the whole kickoff text and path; missing kickoff → the dot item.
- search: temp home with ~30 runs whose kickoffs/artifacts mention distinct feature
  words; a query for one word returns exactly those runs, prefix match works, AND of
  two words narrows; paging: offset 0/limit 10 then offset 10 returns the next ones;
  `has_more` correct; re-index picks up an edited kickoff; no query pages over all runs.
- `board runs` verb: JSON shape, `--limit` cap, usage error on a bad offset.

## Done when

- `/tmp/chunked-gate.sh tests/unit/test_ide_pages_run.py tests/unit/test_ide_pages_search.py tests/unit/test_board_cmd.py`
  passes (run tests in chunks — a single long pytest process is killed by the machine's
  CPU guard); paste its last line.
- On `/Volumes/docker-ssd/Migration/Development/researcher/.mini-ork`:
  `bin/mini-ork board runs --query <a word from a real run title> --home …` returns
  matches; paste the timing of a first (cold) and a second (warm) call.
- ruff clean on touched files; diff touches only files in scope.
