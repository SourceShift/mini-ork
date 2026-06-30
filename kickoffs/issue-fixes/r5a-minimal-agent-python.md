# R5a: native-Python minimal agent module (standalone, not yet routed)

## Context
Epic `docs/epics/EPIC-cloud-exec-runtime-sandbox.md`, phase R5 (runs after R2 — done). Bounded
nodes (one-file mechanical fix, doc tweak, "run X and read output") don't need a full Claude/Codex
CLI harness. We reimplement mini-SWE-agent's pattern NATIVELY in Python (NOT vendoring
`mini-swe-agent` or litellm), reusing mini-ork's own `mini_ork.dispatch` provider layer and the
runtime seam. This phase adds the standalone agent module + test only. Wiring it into the
scaffold-tier router + node-executor is R5b.

Reference: `internal-docs/research/impl-analysis/01-runtime-sandbox-swerex-minisweagent.md`.

## Deliverables
1. `mini_ork/agent/minimal.py` — a `MinimalAgent` (linear-history, stateless-action loop):
   - Loop, capped at `max_turns`: build messages → ask the model for the SINGLE next bash command
     (or a completion sentinel) → run that command → append `{role, output, returncode}` to
     messages → repeat. Messages == trajectory (return the full message list).
   - **LLM turn via `mini_ork.dispatch.dispatch(DispatchRequest(...))`** (reuse the existing typed
     dispatch — do NOT add litellm or a new provider stack). System prompt instructs: reply with
     exactly one fenced bash command, or `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` followed by the
     final answer.
   - **Command execution through the runtime seam** so it inherits the backend (local/bubblewrap):
     execute via the bash seam, e.g. `subprocess.run(["bash","-c", f'source "$MINI_ORK_ROOT/lib/runtime/contract.sh"; mo_runtime_exec {shlex.quote(cmd)} {shlex.quote(cwd)} {timeout}'])`.
     Honor `MO_RUNTIME_BACKEND`. Parse rc + stdout. (Do NOT reimplement isolation in Python.)
   - Stop on the completion sentinel OR when `max_turns` is hit (return a clear "max_turns
     exceeded" result, never hang).
   - A small typed result: `{messages, turns, completed: bool, final_output, exit_status}`.
2. `mini_ork/agent/__init__.py` exporting `MinimalAgent` (+ a `run_minimal(task, *, cwd, max_turns, model)` convenience fn).

## Smoke / DoD (must pass)
- `tests/test_minimal_agent_py.py` (pytest; the test gate runs it):
  - Monkeypatch/stub `mini_ork.dispatch.dispatch` so the "model" returns a scripted sequence
    (e.g. turn 1: `echo hi > out.txt`; turn 2: `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT done`).
    Assert the file is created in `cwd`, the loop completes, `turns==2`, `completed is True`.
  - Assert the turn-cap: a stub that never completes stops at `max_turns` with `completed is False`
    and a clear status (no infinite loop).
  - Assert each action is independent (no shared shell state between turns — e.g. a `cd` in turn 1
    does NOT affect turn 2's cwd).
- `python -m pytest -q` overall still green (additive module; nothing imports it yet).
- `python -c "import mini_ork.agent"` works with `pythonpath=["."]` (the I-7 pytest config).

## Constraints (scope guard)
- Add ONLY `mini_ork/agent/minimal.py`, `mini_ork/agent/__init__.py`, `tests/test_minimal_agent_py.py`.
- Do NOT wire into `bin/mini-ork-execute`, the classifier, or `lib/lane_router.sh` (that is R5b).
- No new pip dependency (`mini-swe-agent`, litellm, etc.) — reuse `mini_ork.dispatch` + the bash
  runtime seam only. Default behavior of the system is unchanged (nothing calls this yet).
