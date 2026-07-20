# Gradient native-dispatch requirements audit

Status: completed

## Task: remove the silent Bash-era no-op from native reflection

Status: completed
Last worked on: 2026-07-20
Remaining parts: none for the production caller edge; Bash library/test
retirement is the next ownership unit.

### Requirements from the migration task and docs

1. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. Default gradient extraction calls the native,
   telemetry-aware dispatcher without sourcing `lib/llm-dispatch.sh`.
2. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. Prompt substitution, model selection, timeout/turn
   limits, fenced/truncated recovery, and evidence/confidence defaults persist.
3. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. Reflection defaults use native extract, store, and
   schema operations; deterministic injection remains available for tests.

### Completion audit loop 1

- Technical requirements: native dispatch, telemetry-aware routing, tolerant
  parsing, trace linkage, persistence, and reflection composition are covered.
- Product requirements: a normal public reflection run can now create learning
  gradients instead of swallowing an unimplemented default and reporting green.
- Unsatisfied requirements found: none for the production caller unit.

### Completion audit loop 2

- Re-read the reflection/gradient modules, configuration docs, migration handoff,
  tracker, and caller tests after implementation.
- Twenty-nine combined gradient/reflection/public-reflect tests passed. Pyright
  reported zero errors and Python compilation succeeded.
- A real GLM 5.2 request extracted three valid gradients from a persisted,
  evidence-rich verifier failure. No MiniMax or DeepSeek request ran.
- `mini-ork validate` passed; garden returned zero errors and the pre-existing
  missing `docs/operator/env-vars.md` warning.
- Closure, diff, secret, and provider-policy scans found no unsatisfied caller
  requirement. Bash library and test retirement remains explicitly separate.
