# An interrupted run resumes with one click (and by itself), without --ack-change

## Why (2026-10-08)

`retry_hint` now classifies a run that stopped mid-node as `needs_change.kind ==
"interrupted"`, `retryable: true`, `strategy: "resume"`, `from_node: <node>` (commit 79c116e5,
case 1.6). Three consumers still treat every `needs_change` as "the operator must change
something first", and refuse without `--ack-change`. Only `kind == "lane"` is exempt:

- `mini_ork/recovery/planner.py` ~:929-948 (`recover`): refuses with "Fix it, then rerun with
  --ack-change";
- `mini_ork/cli/board_cmd.py` ~:583-588 (`_act_retry`): returns "needs a change first";
- `mini_ork/ide_pages/outcome.py` ~:480-500: offers "I fixed it — retry" (`--ack-change`).

So the hint's own command, `mini-ork recover <run> --strategy resume`, is refused. Nothing needs
changing for an interruption: resuming IS the fix.

The auto-repair loop (`mini_ork/recovery/auto_repair.py`, `decide`) has no rule for this kind
either. It falls to rule 8 ("anything else") → `human`.

## Files in scope (touch ONLY these)

- `mini_ork/recovery/retry_hint.py`: ONLY a new module constant (see 1)
- `mini_ork/recovery/planner.py`: ONLY the `needs_change` gate (~:929-948)
- `mini_ork/cli/board_cmd.py`: ONLY the `needs_change` gate in `_act_retry` (~:583-588)
- `mini_ork/ide_pages/outcome.py`: ONLY the retryable-hint action branch (~:480-500)
- `mini_ork/recovery/auto_repair.py`: ONLY `decide` (one new rule)
- `tests/unit/test_interrupted_resume.py` (new)

Do NOT touch any other file.

## Changes (exact)

1. **`retry_hint.py`:** add `NO_CHANGE_KINDS = frozenset({"interrupted"})`, with the comment
   "needs_change kinds whose retry needs no change: re-running the step IS the fix".
2. **`planner.py` gate:** a `needs_change` whose kind is in `retry_hint.NO_CHANGE_KINDS` passes
   without `--ack-change`, the same way a satisfied lane pin does. Use the constant; do not
   hard-code the string.
3. **`board_cmd.py` `_act_retry`:** the same exemption.
4. **`outcome.py`:**
   - A retryable hint whose `needs_change.kind` is in `NO_CHANGE_KINDS` gets ONE primary
     button: `Resume from <from_node>` → `S.cli("board", "retry", run.id, confirm=f"Resume
     {run.id} from {node}?")`, with NO `--ack-change`.
   - Leave the other branches unchanged: lane, generic needs-change, resume-cost.
5. **`auto_repair.decide`:** add a rule right AFTER the lane rule (3) and BEFORE the infra rule
   (4). It applies when the hint's kind is `"interrupted"`:
   - **Killed by a person** (`task_runs.notes` starts with `"killed-by-user"`, written by
     `web/control.py` `board kill`) → `action = "none"`, reason "killed by the user — not
     auto-resumed".
   - **Otherwise** → `action = "infra"`, `from_node = hint.from_node`, reason "interrupted
     during <node> — resume it".

   The stop rules (2) still apply first, unchanged.

## Tests (`tests/unit/test_interrupted_resume.py`; temp home + state.db like `tests/unit/test_auto_repair.py` and `tests/unit/test_retry_hint_interrupted.py`)

Note: `tests/conftest.py` sets `MO_AUTO_REPAIR=0` for every test. A test of `decide` must call
`monkeypatch.delenv("MO_AUTO_REPAIR", raising=False)` first.

- **recover:**
  - an interrupted hint and no `--ack-change` → `planner.cli_main` dispatches (inject
    `execute_fn`; assert it was called);
  - a `kind: "code"` hint without `--ack-change` → still refused (rc 1, not dispatched).
- **board:** `_act_retry` with an interrupted hint and no ack → does NOT return "needs a change
  first".
- **outcome:** a failed run whose hint is interrupted → the actions include "Resume from
  implementer", and its cli args do not contain `--ack-change`.
- **auto-repair:**
  - an interrupted hint → `action == "infra"`, with `from_node` = the hint's node;
  - the same run with `notes = "killed-by-user"` → `action == "none"`.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_interrupted_resume.py tests/unit/test_auto_repair.py tests/unit/test_retry_hint_interrupted.py tests/unit/test_recover_revival.py tests/unit/test_ide_pages_outcome.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check` on the touched files → clean.
- `git diff --stat` touches only the files in scope.
