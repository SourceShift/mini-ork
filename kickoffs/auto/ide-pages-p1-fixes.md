# IDE pages — P1 fixes from the panel review (actions, confirms, honest numbers)

## Goal

The panel review of `board page` (`docs/reviews/2026-10-06-ide-pages-review.md`)
found IDE buttons that fail or do the wrong thing, and numbers that are not real.
Fix the P1 items below. The IDE runs every `cli` action as
`mini-ork <args> --home <home>` and parses its stdout.

## Files in scope

- `mini_ork/cli/board_cmd.py` — new verbs
- `mini_ork/ide_pages/spec.py` — `cli(home=...)`, `bars` clamp
- `mini_ork/ide_pages/{run,runs,changes,autos,verify,nodes,learn,lanes,setup,orch,header}.py`
- `tests/unit/test_board_cmd.py`, `tests/unit/test_ide_pages_*.py` — extend

No other file changes.

## Changes (exact)

1. **New `board` verbs** in `board_cmd.py`, each printing one JSON object
   (`{"ok": true, ...}` or `{"ok": false, "error": "..."}`), exit 0/1:
   - `board kill <run_id>` → `mini_ork.web.control.kill_run(home, db_for(home), run_id)`
   - `board resume <run_id>` → `mini_ork.web.control.resume_cost_run(home, run_id, approver="ide")`
   - `board gate approve|reject <inbox_id> [--note TEXT]` →
     `mini_ork.gates.oversight_inbox.resolve(int(id), "approved"|"rejected", review_note=note, db_path=home/"state.db")`;
     `False` → `{"ok": false, "error": "not pending"}`.
   Add them to the verb `choices`, the module docstring and the usage line.
2. **`--home`-less commands.** `spec.cli(*args, confirm=None, home=True)`: when
   `home=False` the action carries `"home": false` (the IDE then sets
   `MINI_ORK_HOME` in the environment instead of appending `--home`). Use
   `home=False` for every action whose subcommand has no `--home` flag:
   `nodes ping|doctor` (`nodes.py:111,129`), `sandbox-gc` (`nodes.py:245`),
   `bugs sweep|promote` (`learn.py:319-320`), `traceotter` (`learn.py:331`),
   `usage-report` (`lanes.py:440`). Check each against its argparse before
   deciding; do not guess.
3. **Confirm before destructive or costly one-click actions:**
   `run.py:374` Merge (`"Merge this run's branch into <base>?"`),
   `changes.py:106` Merge, `autos.py:103` Run now (`"Start a run of <recipe> now?"`).
4. **Buttons that only typed prose → real verbs:**
   - `verify.py` human gates (≈ lines 405-415): Approve/Reject become
     `cli("board","gate","approve"|"reject",str(id), confirm=...)` (primary/danger).
   - `runs.py` Controls list: cost-paused runs get `Resume` →
     `cli("board","resume",id)`; in-flight runs get `Kill` →
     `cli("board","kill",id, confirm="Kill <id>? SIGTERM, then SIGKILL after 2 s.")`
     (kind danger) next to Stop. Same Kill button in `run.py` next to Stop.
5. **Honest numbers:**
   - `setup.py:36` and `orch.py:35-47` open `state.db` read-only (`mode=ro`) —
     use `mini_ork.web.deps.db_for(home)` like the other pages (a ro open of a
     WAL db with no `-shm` fails and the page shows zeros).
   - `header.py`: when `<home>/state.db` does not exist, `today_usd` is `null`
     (not `0.0`).
   - `spec.bars`: clamp to `[0, 100]`, not `[1, 100]` — a 0 is drawn as empty.

## Tests

- `test_board_cmd.py`: `board kill|resume|gate` on a temp home — unknown run →
  `ok: false`; gate approve on a pending `mo_inbox_gates` row → `ok: true`, second
  call → `ok: false`; usage error (exit 2) without an id.
- New parametrized test in `tests/unit/test_ide_pages_actions.py`: build every
  page/tab on a temp home, collect every `cli` action, and for each one assert the
  subcommand exists in `mini_ork.cli.main`'s registry and — when `home` is not
  `false` — that its parser accepts `--home` (run it with `--help`-free parsing:
  invoke `bin/mini-ork <args> --home <tmp>` only for the `board` verbs; for other
  subcommands assert by importing their argparse and calling `parse_known_args`).
- Existing page tests updated for the new buttons.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_ide_pages_*.py tests/unit/test_board_cmd.py` passes.
- `ruff check mini_ork/ide_pages mini_ork/cli/board_cmd.py tests/unit/test_ide_pages_*.py tests/unit/test_board_cmd.py` is clean.
- The diff touches only the files in scope.
