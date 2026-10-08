# An interrupted run is "resume from <node>", not "Failed at ?; the cause was not classified"

## Why (live, 2026-10-08)

Run `run-orchestrator-scaffold-20261008124718` (framework-edit) went through two revise rounds.
In round 3 its dispatcher stopped while the implementer was running:
- `run_events` has `node_start implementer` at 1791458537 and NO matching `node_end`;
- the task row ended `failed`.

`retry_hint.compute` found no failed verifier, no reviewer verdict, no failed lane row and no
provider trouble. So it fell to case 5 and wrote `failed_node: null`, `retryable: false`,
`"Failed at ?; the cause was not classified"`. The IDE showed that to the user.

The evidence was there all along: a node that STARTED and never ENDED, on a terminal run, is an
interruption. The cheap, correct retry is `mini-ork recover <run> --strategy resume`, which
re-runs that node (recover already closes dangling `node_start` rows).

## Files in scope (touch ONLY these)

- `mini_ork/recovery/retry_hint.py`: one new case function and its call in `compute`, plus
  the `HINT_VERSION` bump.
- `tests/unit/test_retry_hint_interrupted.py` (new)

Do NOT touch any other file.

## Changes (exact)

1. **New `_case_interrupted(home, run_id) -> dict | None`.**
   - Read the events with the existing `_run_node_events(home, run_id)`. Return `None` when it
     returns `None`.
   - Walk `node_start` / `node_end` in order, tracking open nodes by `payload.node_id`: a start
     opens the node, an end closes it.
   - An open node at the end of the walk is a node that started and never ended. Several open
     → take the one started LAST. None open → return `None`.
   - Return, in the same key shape as the other cases:

     ```
     {"version": HINT_VERSION, "run_id", "failed_node": node, "retryable": True,
      "strategy": "resume", "from_node": node,
      "needs_change": {"kind": "interrupted",
                       "summary": f"Interrupted during {node}: the run stopped before the step finished. Resume from it.",
                       "detail": _tail_log(run_dir, node), "evidence": "node_start without node_end"},
      "notes": [], "command": _build_command("resume", run_id), "computed_at": _now_iso()}
     ```
2. **Call it in `compute`** right after the withheld-publish case (1.5) and BEFORE the reviewer
   case. The interruption is the run's most recent fact; an older round's reviewer verdict must
   not shadow it.
3. **Bump `HINT_VERSION` to 3**, with a one-line `v3` note next to the existing `v2` note, so
   cached hints written by the old rules self-heal.

## Tests (`tests/unit/test_retry_hint_interrupted.py`; temp home + state.db + run dir fixtures like `tests/unit/test_retry_hint.py`)

- **Dangling node.** A failed run whose events are:
  - `start planner` / `end planner done`;
  - `start implementer` / `end implementer done`;
  - `start reviewer` / `end reviewer verdict_revise`;
  - `start implementer` with no end.

  → `failed_node == "implementer"`, `retryable is True`, `strategy == "resume"`, `kind ==
  "interrupted"`, and the command contains `--strategy resume`.
- **Same run, but the last implementer ended `error` and rollback ran (start + end).** → NOT
  interrupted: the case returns `None`, and `compute` falls through to the old rules.
- **Two open nodes** (parallel lenses, both started, neither ended) → the one started last.
- **No `run_events` table** → the case returns `None`, and `compute` does not raise.
- **`HINT_VERSION == 3`.**

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_retry_hint_interrupted.py tests/unit/test_retry_hint.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

`tests/unit/test_retry_hint.py::test_run_page_retry_section_for_case_4` has a known
order-dependent failure in the full file that passes alone. If it is the only failure, rerun it
alone and paste both results.

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/recovery/retry_hint.py tests/unit/test_retry_hint_interrupted.py` → clean.
- `git diff --stat` touches only the files in scope.
