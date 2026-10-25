# ECOSYSTEM §6 · K5: the cost pause actually stops the run, and resume restarts it

## Goal

The per-run cost pause is documented as a hard stop ("blocks next call,
operator-resumable", `docs/reference/FEATURE-INVENTORY.md`; "the dispatch halts
until resume", `mini_ork/dispatch/cost_pause.py` docstring; "pauses before its
next LLM call", `mini_ork/web/routes/control.py`). It does not stop anything:

- `cost_pause.check()` writes `<run_dir>/.cost-pause` and returns 2. Its only
  caller, `charge_node_cost` in `mini_ork/cli/execute.py`, sets
  `MO_NODE_FINISH_REASON=paused_for_approval` inside a `try/except: pass`, and
  that reason is only read for nodes that did not succeed.
- Before each node, `mini_ork/cli/execute_handlers.py` checks only
  `.stop-requested`. Nothing in the dispatch path reads `.cost-pause`.
- `mini-ork resume` (`mini_ork/cli/resume.py::resume`) and the web
  `resume_cost_run` (`mini_ork/web/control.py`) delete the sentinel and write an
  audit row, but never restart the run (it never stopped).

Make the sentinel stop new dispatches through the existing pause drain, and make
both resume paths restart the run with `recover --strategy resume`, the way
`mini-ork approvals` resumes a review-parked run.

`mini_ork/dispatch/cost_pause.py` is in the frozen kernel: do not modify it.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. `mini_ork/cli/execute_handlers.py`, in the pre-dispatch gates, directly
   after the `.stop-requested` check: if `<run_dir_eff>/.cost-pause` exists,
   - publish `MO_NODE_FINISH_REASON=paused_for_approval`,
   - print `  [cost-pause] .cost-pause present — not dispatching node_id=<id>; resume with: mini-ork resume <run_id>`
     to stderr,
   - return `(node_review.REVIEW_PAUSE_RC, "paused_for_approval")`
     (import `REVIEW_PAUSE_RC` the same way the intervention-gate park does).
   The executor's existing pause drain then stops new nodes, lets in-flight
   nodes finish, and ends the run with rc 9; the lifecycle already skips
   auto-repair, verify and reflect for rc 9.
2. `mini_ork/cli/resume.py::resume`: after the sentinel is removed and the
   audit row written, restart the run by calling
   `mini_ork.cli.approvals_cmd._spawn_recover(home_path, run_id, ["--strategy", "resume"], log_path=<run_dir>/cost-resume.log)`.
   - `home_path` is the `Path` of the run home the function already resolves
     the run dir from.
   - On `approvals_cmd._LauncherMissingError` (or any `OSError`), do not raise:
     append `resume with: mini-ork recover <run_id> --strategy resume` to the
     returned stdout message.
   - Add a keyword argument `restart: bool = True`; `main` exposes it as
     `--no-restart` (clear + audit only, today's behaviour). Update `HELP`/usage
     text for the new flag.
   - The returned stdout line gains `; restarting (log: <path>)` when a restart
     was spawned.
3. `mini_ork/web/control.py::resume_cost_run`: after clearing the sentinel and
   writing the audit row, spawn the same restart (same helper, same args, same
   log name) and add `"restarted": True/False` and `"log": <path or None>` to
   the returned dict. A spawn failure sets `"restarted": False` and
   `"hint": "mini-ork recover <id> --strategy resume"`; it never raises.

## Files in scope (touch ONLY these)

- `mini_ork/cli/execute_handlers.py`
- `mini_ork/cli/resume.py`
- `mini_ork/web/control.py`
- `tests/unit/test_eco6_k5_cost_pause.py` (new)
- existing tests of `resume.py` / `web/control.py` **only** where they assert
  the exact old stdout line or the exact old dict keys (extend the expectation,
  don't weaken it)

Do NOT modify `mini_ork/dispatch/cost_pause.py`, `mini_ork/cli/main.py`,
`mini_ork/cli/execute.py`, or `mini_ork/cli/approvals_cmd.py`.

## Tests (`tests/unit/test_eco6_k5_cost_pause.py`, hermetic; no real spawn)

- **The gate stops dispatch.** Call the pre-dispatch path the way existing
  `execute_handlers` tests do for `.stop-requested` (find one with
  `git grep -n "stop-requested" tests/`), with `<run_dir>/.cost-pause` present:
  the result is `(REVIEW_PAUSE_RC, "paused_for_approval")` and the lane
  dispatch function is never called (monkeypatch it to record calls).
- **No sentinel, no change.** Without the file the same call proceeds to
  dispatch.
- **CLI resume restarts.** With a tmp home containing `runs/<id>/.cost-pause`,
  monkeypatch `approvals_cmd._spawn_recover` to record its args and return
  `(123, "<log>")`. `resume.main([<id>])` returns 0, the sentinel is gone, the
  audit file has one row, and the recorded args are
  `(home, <id>, ["--strategy", "resume"])`.
- **`--no-restart`** clears and audits but never calls `_spawn_recover`.
- **Launcher missing.** `_spawn_recover` raising `_LauncherMissingError` → rc 0,
  stdout contains `mini-ork recover <id> --strategy resume`.
- **Web resume restarts.** `resume_cost_run(home, <id>, "op")` returns
  `ok=True, restarted=True` with the same recorded args; with the spawn
  raising, `restarted=False` and a `hint`.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k5_cost_pause.py $(git grep -l -E "cost_pause|cost-pause|resume_cost_run|cli\.resume|from mini_ork.cli import resume" -- tests | tr '\n' ' ')`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k5_cost_pause.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k5-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k5-junit.xml`

## Success criteria

- The verification command passes.
- `ruff check --select F,E9 mini_ork/cli/execute_handlers.py mini_ork/cli/resume.py mini_ork/web/control.py` is clean.
- A run with no `.cost-pause` file dispatches exactly as before.
