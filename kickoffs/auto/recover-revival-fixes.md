# `mini-ork recover` can actually revive a run: close the dead attempt, scope the watchdog, never leave a zombie

## Why (live, 2026-10-08)

The user's rule is to revive failed runs from their failed node, never to shelve or re-create
them. The first revival failed:

```
mini-ork recover ide-orca-b2a-triage-20261008104922 --from-node test --force --ack-change
[mini-ork-recover] dispatching closure from node=test (4 nodes to rerun)
  [timeout] stale heartbeat detected before node_id=reviewer: publisher	1791451525130
```

1. **The original attempt left a dangling `node_start` for `publisher`** (no `node_end`; its
   process is long dead). The stale-heartbeat watchdog
   (`mini_ork/cli/execute.py` `_watchdog_stale_heartbeat`, ~:1647) scans EVERY node of the run.
   It found that dead node's heartbeat older than `MO_HEARTBEAT_TIMEOUT_S` and aborted the
   recovered reviewer with `timeout`. Any revival of a run whose previous process died
   mid-node aborts the same way, which turns a cheap checkpoint-reuse recovery into a failure.
2. **The aborted recovery left `task_runs.status = 'executing'`** with no live process: a zombie
   row the board shows as working, which a peer session had to reap by hand.

A helper already exists: `mini_ork/web/control.py` `_close_dangling_node_events(db, run_id)`
inserts a synthetic `node_end` for every `node_start` lacking one (`board kill` uses it).

## Files in scope (touch ONLY these)

- `mini_ork/recovery/planner.py`: ONLY `cli_main`
- `mini_ork/cli/execute.py`: ONLY `_watchdog_stale_heartbeat`
- `tests/unit/test_recover_revival.py` (new)

Do NOT touch `execute_handlers.py`, `publisher.py` (claimed by another worktree) or
`web/control.py` (import its helper; do not edit it).

## Changes (exact)

1. **`cli_main`: close the dead attempt before dispatching.** After `main(argv,
   handoff=handoff)` succeeds and before `execute_fn(exec_argv)`, close every dangling node of
   the run: a `node_start` with no `node_end`.
   - Use `web.control._close_dangling_node_events` (lazy import; it needs a `StateDB`: build one
     from `handoff["db_path"]`), or an equivalent local insert if importing it is impractical.
   - The synthetic `node_end` payload should say `finish_reason: "abandoned"` (pass it through
     if the helper allows; otherwise the helper's CRASH marker is acceptable).
   - Print `[mini-ork-recover] closed <n> dangling node(s) from the previous attempt`.
   - Fail-soft: an error here is printed and does not stop the recovery.
2. **Never leave a zombie.** After `exec_rc = execute_fn(exec_argv)` (inside the existing
   try/finally): if the run's `task_runs.status` is still `executing`/`running`, mark it
   `failed` through `mini_ork.orchestration.run_reaper._mark_failed` (the compare-and-set
   writer), with note `"recover: execute exited rc=<rc> without a terminal status"`.
   - This applies when `exec_rc != 0`, and also when execute raised. Use the `finally` and read
     the row first.
   - Fail-soft.
3. **The watchdog judges only the current attempt.** In `_watchdog_stale_heartbeat`, ignore
   heartbeats older than this execute process's start. Record
   `_EXEC_STARTED_MS = int(time.time() * 1000)` once at module import or on the first call, or
   read `MO_EXEC_STARTED_MS` when set. A node whose last heartbeat predates the start belongs to
   a previous process, and the recovery closes it (change 1). Everything else is unchanged.

## Tests (`tests/unit/test_recover_revival.py`; temp DB via `mini_ork.stores.migrate.init_db`, like `tests/unit/test_ide_pages_outcome.py`)

- **Watchdog:** a run with a `node_start` for `publisher` whose `last_heartbeat_at` is 1 hour
  old and that has no `node_end`:
  - `_watchdog_stale_heartbeat` returns `""` when the process start is after it;
  - it still returns the node when the heartbeat is newer than the start and older than the
    timeout (a real hang in the current attempt).
- **`cli_main`** with a stub `execute_fn` and a handoff to a temp run:
  - dangling `node_start`s get a `node_end` before `execute_fn` is called (assert inside the
    stub);
  - when the stub returns 1 and leaves the row `executing`, the row ends `failed`;
  - when the stub sets the row `published` and returns 0, it is untouched;
  - when the stub raises, the row ends `failed` and the exception propagates (or rc 1); keep
    today's contract.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_recover_revival.py tests/unit/test_recover_cli_dispatch.py tests/unit/test_recover_verify.py tests/unit/test_lane_repair_resume.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/recovery/planner.py mini_ork/cli/execute.py tests/unit/test_recover_revival.py` → clean.
- `git diff --stat` touches only the files in scope.
