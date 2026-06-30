# R0b: route the executor through the runtime seam (behavior-preserving)

## Context
Epic `docs/epics/EPIC-cloud-exec-runtime-sandbox.md`, phase R0b. R0a already landed the seam:
`lib/runtime/contract.sh` (`mo_runtime_exec "<command>" "<cwd>" [timeout_s]`, plus
`mo_runtime_put/get/start/stop/alive` and a `MO_RUNTIME_BACKEND` factory, default `local`) and
`lib/runtime/local.sh` (process-group-kill-on-timeout exec). Nothing calls it yet.

This phase routes the executor's actual command execution through `mo_runtime_exec` so the seam
becomes load-bearing — WITHOUT changing any behavior. With the default `local` backend it must be
byte-for-byte identical to today.

## Hard delivery-safety constraints (see the epic's "Delivery-safety constraints" section)
- Default `MO_RUNTIME_BACKEND` unset ⇒ `local` ⇒ identical behavior. No new latency, no container
  boot, no extra fork on the hot path.
- Do NOT change verifier / reviewer / publisher / rollback semantics. Only the mechanism by which
  a shell command is run changes (from inline `cd`+`bash`/subshell to `mo_runtime_exec`).
- Concurrency-safe: no new global state or lock; per-run only.
- researcher (~29 concurrent runs, macOS, code-fix/framework-edit/epic-runner) must be unaffected.

## Deliverables
1. In `bin/mini-ork-execute`, replace the direct command-execution call sites with
   `mo_runtime_exec`:
   - the verifier-ref runner `_run_verifier_ref` (the `( cd "$_verify_cwd" && ... bash "$_script" )`
     subshell) → `mo_runtime_exec "bash '$_script'" "$_verify_cwd" "$timeout"` (preserve the exact
     cwd resolution, env, and exit-code propagation it does today).
   - any other inline `cd <dir> && bash`/CLI command-run spots in the node-dispatch path that are
     plainly "run this shell command in this cwd" (do the minimal set; leave LLM-dispatch via
     `lib/llm-dispatch.sh` alone — that is a separate seam).
   - Source `lib/runtime/contract.sh` once near the top of `bin/mini-ork-execute`
     (`[ -f ... ] && source ...`), defensive.
2. Behavior parity: with no `MO_RUNTIME_BACKEND` set, the resolved backend is `local` and output,
   exit codes, cwd, env, and timeout behavior are unchanged.

## Smoke / DoD (must pass)
- `bash -n bin/mini-ork-execute lib/runtime/*.sh` clean.
- `bash tests/unit/test_runtime_contract.sh` still green (R0a test untouched).
- `pytest` (tests/) still 118 passed — no behavior change.
- A new/extended bash test asserting a verifier node run through `_run_verifier_ref` produces the
  same verdict/exit code as before (e.g. a trivial passing verifier returns pass; a failing one
  returns fail), proving the routing preserved semantics.
- Existing recipe smoke unaffected: a `code-fix` or `framework-edit` dry path still classifies/
  plans/executes as before.

## Constraints (scope guard)
- Touch ONLY `bin/mini-ork-execute` (+ the seam source line) and a test. Do NOT modify
  `lib/runtime/*` (R0a is done), recipes, or `lib/llm-dispatch.sh`.
- No new external deps. macOS-compatible. Keep `local` the default; do not introduce bubblewrap/
  docker here (those are R2/R3).
