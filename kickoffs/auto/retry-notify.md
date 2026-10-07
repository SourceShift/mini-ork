# Tell a failed run's owner what to fix and how, then continue when they say it's fixed

## Why

`mini_ork/recovery/retry_hint.py` (merged; `load_or_compute(home, run_id, *, write=True)`, `compute(home, run_id)`) classifies a failed run: retryable or not, and the change
needed first (`needs_change.kind` = environment / credentials / budget / code / unknown). Nobody is
told. The researcher run `run-le-1791359434-64879-1` failed four cycles on the same missing
backend env var while its implementer's log said exactly what to set. This run closes the loop:
**tell the run's owner what to fix and how → owner fixes it → owner says "fixed" → mini-ork
continues the run** (via `board retry … --ack-change`, which reuses finished nodes).

## Files in scope

- `mini_ork/recovery/retry_notify.py` (new)
- `mini_ork/cli/main.py` (two small hooks only — see 3 and 5)
- `mini_ork/cli/board_cmd.py` (`_act_gate` + `retry` verb tweaks only)
- `mini_ork/acp/task_state.py` (needs-you state only)
- `tests/unit/test_retry_notify.py` (new)

Do NOT edit `mini_ork/cli/execute.py`, `execute_handlers.py`, `mini_ork/recovery/planner.py`,
`plan.py`, or `retry_hint.py`'s classification.

## 1. Owner (`retry_notify.owner(home, run_id) -> {"kind": "...", "id": "...", "label": "..."}`)

- `<run_dir>/owner.json` when present (written by 3).
- Else infer: `MINI_ORK_PARENT_RUN_ID`-style parent in `run_events.parent_run_id` → `run:<parent>`;
  kickoff path under `<home>/rsi/<name>/` → `loop:<name>`; kickoff path under
  `<home>/automations/` or an automation record naming the run → `automation:<id>`;
  otherwise `user:<git config user.email, else $USER>`.

## 2. How to fix (`retry_notify.fix_steps(hint) -> list[str]`, deterministic, no LLM)

- environment: extract env var names (`[A-Z][A-Z0-9_]{3,}` within 40 chars of "not set", "unset",
  "export", "missing") from `detail` + `notes`; extract the surfaces from the verifier's smoke spec
  `needs:` list when the run dir names one (`spec` key in the verifier JSON). Steps, e.g.
  "Set ONBOARDING_DEMO_BOOK_PATH in the backend's environment (see <evidence>)",
  "Restart the backend so it picks the variable up", "Confirm: mini-ork board retry <run> --ack-change
  (or approve the inbox item)". Unknown names → "Read <evidence> — it says: <first line of detail>".
- credentials: name the lane/provider from the failed node's attempt row; "update the key in
  config/secrets.local.sh (or the provider env var), then confirm".
- budget: "raise the cap or approve the spend: mini-ork resume <run>".
- code: "Start a revision run with these reasons: …" + the reasons; no confirm-to-retry step.
- unknown: "Read <evidence>" + last 5 log lines.

## 3. Record + notify at the end of `mini-ork run` (`main.py`)

- At run start, write `<run_dir>/owner.json` from `MO_RUN_OWNER` (`kind:id`) when set, else the
  inference in 1.
- After the verify step, when the run failed: `retry_notify.notify(home, run_id)` (best-effort,
  never changes the run's exit code):
  - compute the hint (`load_or_compute`); if `needs_change` is set and `retryable` is true (or kind
    is `code`), write `<run_dir>/NEEDS-CHANGE.md` (owner, summary, detail, steps, evidence, the
    continue command) and enqueue ONE `mo_inbox_gates` row via
    `mini_ork.gates.oversight_inbox.enqueue(gate_id="retry_precondition", feature=<run_id>,
    phase="retry", context={hint, steps, owner}, blocks_dispatch_for=<kickoff path>)` — skip if a
    pending row for this run already exists; store its `inbox_id` in `<run_dir>/retry-gate.json`.
  - print a block to stdout: `── needs a change before this run can continue ──`, the summary,
    numbered steps, and key/value lines `needs_change=<kind>`, `retry_hint=<path>`,
    `retry_inbox_id=<id>` (loops parse key=value lines).

## 4. Continue when the owner says it's fixed (`board_cmd.py`)

- `board gate approve <inbox_id>` on a `retry_precondition` row → after resolving, run the same
  path as `board retry <run> --ack-change` (detached) and return its pid/log in the payload.
  `board gate reject` → resolve and write `{"abandoned": true}` into `retry-gate.json`.
- `board retry <run> --ack-change` resolves that run's pending `retry_precondition` row as
  approved (note "retried via board retry").

## 5. Don't start a duplicate while a fix is pending (`main.py`, run start)

Before dispatch, if a pending `retry_precondition` row has `blocks_dispatch_for` == this run's
kickoff path (realpath), refuse: print the pending run id, summary and the continue command, exit
75. `MO_IGNORE_PENDING_FIX=1` overrides. (Loops that relaunch the same kickoff stop paying for
cycles that can only fail the same way.)

## 6. Show it in the IDE (`task_state.py`)

A failed run whose `retry-gate.json` names a still-pending row reads as state `needs_you` with step
`needs a fix: <summary (60 chars)>` — so it lands in the Threads "Needs you" group and the header
count. Cheap: one small JSON read, plus one indexed SELECT on `mo_inbox_gates` only when the file
exists.

## Tests (`tests/unit/test_retry_notify.py`; tmp homes, no LLM, spawn monkeypatched)

Owner inference (each branch + owner.json wins); fix steps per kind including the env var
extraction on the real `run-le-1791359434-64879-1` texts (copy the verifier reason and the
implementer log line into a fixture); notify writes NEEDS-CHANGE.md + exactly one gate on repeat;
approve → retry spawned with `--ack-change`; reject → abandoned; `board retry --ack-change`
resolves the gate; the start guard refuses with 75 and `MO_IGNORE_PENDING_FIX=1` passes;
task_state shows needs_you for a pending gate and not after resolution.

## Done when

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_retry_notify.py tests/unit/test_retry_hint.py tests/unit/test_board_cmd.py tests/unit/test_acp_task_state.py` passes — paste the summary line.
- `ruff check` on the touched files is clean.
- Read-only proof: print `fix_steps(load_or_compute(<researcher home>, 'run-le-1791359434-64879-1', write=False))`
  — must name `ONBOARDING_DEMO_BOOK_PATH` and the backend restart. Do not call `notify` on a real home.
- Diff touches only files in scope.

## Notes for the implementer

- Run the Done-when pytest yourself and paste the real summary line; `python3 -m pytest` must pass
  too (compare `sys.executable`, never a hard-coded interpreter name).
- The read-only proof is a CLI/library read, not "running the BE or FE" — it is required.
- `bin/mini-ork` runs the MAIN engine; prove with `PYTHONPATH=$PWD python3.11 …` in this worktree.
