# ECOSYSTEM §6 · K12: `mini-ork epics archive` — close an epic that will never run

## Goal

The epic queue holds 23 `not started` epics; an audit found 18 of them are
already delivered elsewhere, target files removed in the 2026-07 bash removal,
belong to another repo, or have no kickoff at all. `mini-ork epics status`
(`mini_ork/cli/epics.py::_status`) can only set one of the CHECK-allowed
statuses (`not started, in progress, in review, done, blocked, escalated`), so
an obsolete epic can only be mislabelled `done` or left in the queue, where the
supervisor's opt-in dispatcher (R8) would eventually start a paid run for it.

The table already has the right column: `epics.archived_at`. The scheduler
(`mini_ork/scheduler.py` `pick_ready`), the epic graph and the supervisor all
filter `archived_at IS NULL`, and `epics list` hides archived rows. Add the
operator verb that sets it.

All paths are relative to the repository root.

## Mechanism (exact spec)

In `mini_ork/cli/epics.py`:

1. Add `_archive(rest: list[str], db: str) -> int` for
   `epics archive <epic_id> --note TEXT`:
   - `--note` is required; missing or whitespace-only → stderr
     `archive: --note is required`, rc 2, DB untouched (same rule as `status`).
   - Unknown extra arguments → `archive: unknown argument(s): …`, rc 2.
   - Unknown epic id → `archive: no such epic: <id>`, rc 1.
   - An epic with `status = 'in progress'` is refused: stderr
     `archive: <id> is in progress — stop its run first`, rc 1.
   - An already-archived epic is a no-op: stdout
     `<id> already archived at <ts>`, rc 0.
   - Otherwise, in one transaction: set `archived_at` to
     `strftime('%Y-%m-%dT%H:%M:%fZ','now')` and append
     ` [operator: archived: <note>]` to `notes` (handle `NULL` notes). Do not
     change `status` (the `trg_epics_no_revert_done` trigger stays satisfied).
     Print `archived <id> (status <status>)`, rc 0.
   - Use `PRAGMA busy_timeout=5000` like `_status`.
2. Wire it in `main`: `if sub == "archive": …` (epic id required, same error
   shape as `status`).
3. Add the line to `_USAGE`:
   `  archive <epic_id> --note TEXT   Hide an epic that will never run (sets archived_at)`.

## Files in scope (touch ONLY these)

- `mini_ork/cli/epics.py`
- `tests/unit/test_eco6_k12_epics_archive.py` (new)

## Tests (`tests/unit/test_eco6_k12_epics_archive.py`, hermetic tmp DB)

Create the DB the way existing epics CLI tests do (find one with
`git grep -n "epics.main\|from mini_ork.cli import epics" tests/`), then seed
epics `a` (`not started`), `b` (`in progress`), `c` (`done`).

- `main(["archive", "a", "--note", "obsolete"], db=…)` → rc 0; row `a` has a
  non-null `archived_at`, `status` still `not started`, `notes` ends with
  `[operator: archived: obsolete]`; `main(["list"], db=…)` output no longer
  contains `a`.
- `mini_ork.scheduler.pick_ready` (or `epic_graph.ready_now`, whichever the
  existing tests use) does not return `a`.
- Archiving `c` (`done`) → rc 0 and `status` stays `done`.
- Archiving `b` (`in progress`) → rc 1, row unchanged.
- No `--note` → rc 2, row unchanged. Unknown id → rc 1.
- Archiving `a` twice → second call rc 0 with `already archived`.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k12_epics_archive.py $(git grep -l -E "cli import epics|cli\.epics|epics\.main" -- tests | tr '\n' ' ')`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k12_epics_archive.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k12-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k12-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/cli/epics.py` is clean.
