# IDE data: per-lane health in the status bar header, and a "steer this run" composer on a running run's Story

## Why

The plan is `/Users/admin/.claude/plans/twinkling-churning-feather.md`, P4 and the P1 composer.

- **Per-lane status bar.** The IDE status bar should show per-lane mini-bars: today's calls,
  failures, and $ against the daily budget. That way a dead lane (today: codex rejecting its
  model and quota-limited) is visible at a glance instead of discovered from failed runs.
  `board --json`'s `header` (built by `mini_ork/ide_pages/header.py` `header()`) carries no lane
  data.
- **Composer.** A running run's Story tab has no way to steer the agent. The IDE now draws a
  `composer` section (zed `62cb387`), and `spec.composer(title, placeholder, cli)` exists on
  main. The steer verb is `mini-ork board steer <run> --text <text>`, so the composer's `cli`
  is `["board", "steer", <run_id>, "--text"]`, with the typed text appended.

## Files in scope (touch ONLY these)

- `mini_ork/ide_pages/header.py`: ONLY a new `_lanes(db)` helper and its call in `header()`
- `mini_ork/ide_pages/run.py`: ONLY `_story_tab`, to add the composer for a running run
- `tests/unit/test_ide_header_lanes_composer.py` (new)

Do NOT touch any other file.

## Changes (exact)

1. **`header._lanes(db_path) -> list[dict]`.** ONE SQL query over `llm_calls` for the last 24 h
   (`ts` is ISO text: compare against an ISO cutoff).
   - Group by the lane label: `actor` when present, else `provider`.
   - Per lane: `{"lane", "calls": int, "failed": int, "usd": float(round 2), "last_error": str}`.
     - `failed` counts rows whose `status` is not `'success'`.
     - `last_error` is the newest failing row's `error_message`, cut to 120 chars.
   - Sort by calls desc and cap at 8 lanes.
   - Fail-soft: missing table or columns → `[]`.
   - Add `"lanes": _lanes(db)` to the dict `header()` returns, only when `db.is_file()`;
     otherwise `[]`.
   - It is polled every few seconds, so it must stay ONE indexed-friendly query with no
     per-lane queries. Note in the docstring that it uses the `ts` index if one exists.
2. **`run._story_tab`.** When the run's status is running/executing (use the same running test
   the page already uses for its live chip), append at the END:

   ```
   S.composer("Steer this run", "Tell the running agent what to change… (Enter to send)", ["board", "steer", run.id, "--text"])
   ```

   Finished runs get no composer.

## Tests (`tests/unit/test_ide_header_lanes_composer.py`)

- `_lanes` on a temp state.db (migrate with `mini_ork.stores.migrate.init_db` like other
  tests), with seeded rows:
  - two lanes;
  - one failing row with an error message;
  - one row older than 24 h.

  → the right counts, `usd`, `last_error`, and the old row excluded.
- `_lanes` on a db without `llm_calls` → `[]`.
- `header()` includes `lanes`.
- The run page (spec level 2) for a running run → its Story tab ends with a `composer` whose
  `cli == ["board", "steer", <run_id>, "--text"]`. A finished run → no composer. Reuse the
  run-page fixture pattern from `tests/unit/test_ide_pages_run.py`; copy it, do not import
  private test helpers.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_ide_header_lanes_composer.py tests/unit/test_ide_pages_run.py tests/unit/test_board_cmd.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/ide_pages/header.py mini_ork/ide_pages/run.py tests/unit/test_ide_header_lanes_composer.py` → clean.
- **Live proof (read-only).** Paste the output of:

  ```
  bin/mini-ork board --json --home /Volumes/docker-ssd/ps/mini-ork/.mini-ork | python3 -c 'import json,sys;print(json.load(sys.stdin)["header"]["lanes"])'
  ```
- `git diff --stat` touches only the files in scope.
