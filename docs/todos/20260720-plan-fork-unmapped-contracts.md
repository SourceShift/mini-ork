# Plan fork migration blockers — resolved

Status: completed
Last worked on: 2026-07-20

The plan fork cannot retire yet. The pre-retirement suite is green for its
covered cases, but a second requirements audit found Bash-owned behavior that
is absent from both `integration-map.json` and `static-feature-ledger.json`.

## Task: port every omitted planner contract before retirement

Status: completed
Last worked on: 2026-07-20
Remaining parts: none.

1. Profile normalization and question handling — completed
   - Native zero-question normalization, `/dev/tty` input, auto-answering,
     profile mutation, confidence floor, and blocking contracts are covered by
     standalone planner tests.

2. Planner context assembly — completed
   - Learned failure modes, prior-run memory, the planner role pack, generic
     ContextNest fallback, recent sessions, and active state use native Python
     modules with best-effort failure isolation and ordered prompt coverage.

3. Context-pack persistence — completed
   - Non-dry runs persist `context-pack.json` through native
     `context_assemble`; focused tests assert its path and payload.

4. Trace lifecycle writes — completed
   - Native best-effort traces cover running, blocked, fallback, dispatch and
     validation failures, and success. Migration 0054 makes `blocked` a valid
     canonical status and preserves existing trace rows.

5. Strengthen the retirement oracle — completed
   - The durable pre-retirement report remains preserved, the completion ledger
     contains 58 rows, and post-retirement parity, feature acceptance, Pyright,
     ledger shape, and fork closure are green. `bin/mini-ork-plan` is retired.

## Work completed in this attempt

- Replaced the Python planner's Bash `llm-dispatch.sh` subprocess with the
  native `mini_ork.ported.llm_dispatch.llm_dispatch` API.
- Preserved combined stdout/stderr capture and added a standalone native seam
  contract.
- Repointed known executable callers and expanded the plan feature verifier.
- Deleted the Bash entrypoint after the completion audit closed every blocker.
