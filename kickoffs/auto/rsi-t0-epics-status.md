# RSI Tier-0 #4: `mini-ork epics status`, an operator command to correct an epic's status

## Goal

The epic table drifts whenever an epic's work lands outside the scheduler,
for example through a framework-edit run launched by hand. In the primary home:

- `rsi-e1-code-arm` still reads `not started` although its code merged
  (9a698e04). Every `rsi-e2..e8` epic has a hard dependency on it, so all seven
  stay `blocked`.
- `arx-0-scout` reads `done` although its only run produced no diff and was
  rolled back.

`mini-ork epics` has `add`, `set`, `priority` and `retry`, but no way to set a
status. So the only fix today is hand-written SQL, which skips the dependency
cascade that the scheduler runs when an epic is done.

Add one subcommand that sets an allowed status, records why, and cascades
exactly as the scheduler does.

All paths are relative to the repository root.

## Mechanism (exact spec)

In `mini_ork/cli/epics.py`:

1. **The subcommand** `status <epic_id> <new_status> --note TEXT`.
   - `new_status` must be one of the values allowed by the `epics.status`
     CHECK constraint (`db/migrations/0001_core.sql`): `not started`,
     `in progress`, `in review`, `done`, `blocked`, `escalated`.
   - Accept `not-started` / `in-progress` / `in-review` as spellings of the
     spaced forms.
   - Any other value → stderr `status: '<v>' is not one of: <list>`, rc 2.
   - `--note` is **required** (the reason is the audit trail). A missing or
     whitespace-only note → stderr `status: --note is required`, rc 2.
   - An unknown epic id → stderr `unknown epic: <id>`, rc 1.
   - An unknown extra argument → rc 2 with a message naming it.
2. **The update.**
   - Set `epics.status` to the new value.
   - Append ` [operator: <old> -> <new>: <note>]` to `epics.notes` (COALESCE
     the old notes).
   - When the new status is not `blocked`, clear `held_reason`.
   - Then:
     - **`done`:** call `mini_ork.orchestration.epic_graph.on_done(epic_id, db=db)`,
       the same cascade `mini_ork/scheduler.py:_settle` runs on a done verdict.
     - **Any other status:** call no cascade.
   - Print `<epic_id>: <old> -> <new>` on stdout and return 0.
3. **No-op.** Setting the status an epic already has prints
   `<epic_id>: already <status>`, changes nothing (no note appended, no
   cascade) and returns 0.
4. **Usage text.** Add `status` to the module's usage/help text, next to
   `retry`, with a two-line description.
5. **Dispatch.** Route `sub == "status"` the same way the other subcommands
   are routed.

## Files in scope (touch ONLY these)

- `mini_ork/cli/epics.py`
- `tests/unit/test_epics_status_cmd.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_epics_status_cmd.py`: hermetic temp sqlite, in process)

- **Setup.** Build the DB the way `tests/unit/test_mini_ork_epics_py.py` does
  (reuse its schema setup or helper). Create epics `a` (`not started`) and `b`
  (`blocked`), plus a hard `epic_dependencies` row from `a` to `b`, the shape
  `epic_graph.on_done` resolves. Read `mini_ork/orchestration/epic_graph.py:on_done`
  for exactly what it updates, and assert that.
- **`done` cascades.** `status a done --note "merged 9a698e04"`:
  - returns 0;
  - leaves `a` `done`, with notes containing `operator: not started -> done: merged 9a698e04`;
  - leaves the dependency resolved exactly as after a scheduler `done`. Assert
    the state `on_done` produces; do not invent a different one.
- **Other statuses do not cascade.** `status a escalated --note "rolled back"` sets
  `escalated` and calls no cascade. Monkeypatch `epic_graph.on_done` and assert
  it was not called.
- **Spaced and hyphenated forms.** `status a not-started --note x` sets
  `not started`.
- **Rejected inputs.**
  - `status a failed --note x` → rc 2 and the epic is unchanged.
  - A missing `--note` → rc 2.
  - `--note "  "` → rc 2.
  - An unknown epic → rc 1.
- **No-op.** Setting the current status → rc 0, prints `already`, and the notes
  are unchanged.
- **`held_reason`.** It is cleared when moving a `blocked` epic to `not started`.

## Verification commands

- `pytest -q tests/unit/test_epics_status_cmd.py tests/unit/test_mini_ork_epics_py.py tests/unit/test_epics_add.py tests/unit/test_epic_graph_py.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_epics_status_cmd.py --junitxml="${MINI_ORK_RUN_DIR}/epics-status-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/epics-status-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/cli/epics.py` is clean.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
