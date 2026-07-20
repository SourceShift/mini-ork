# Profile-answerer Bash-retirement requirements audit

Status: completed

## Task: make the native profile answerer the sole owner

Status: completed
Last worked on: 2026-07-20
Remaining parts: none for this ownership fork.

### Requirements from the migration task and docs

1. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. The planner is the only production inbound caller
   and already imports `mini_ork.steering.profile_answerer` directly.
2. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. Tests use standalone golden contracts rather than a
   live Bash oracle, and web smoke verifies that the Bash owner is absent.
3. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. The native default follows the latest supported
   commit history: Kimi primary plus one Kimi retry after failure or whitespace.
4. Status: completed
   Last worked on: 2026-07-20
   Remaining parts: none. `lib/profile_answerer.sh` is removed and closure scans
   retain only deliberate absence assertions and migration history.

### Completion audit loop 1

- Technical requirements: native ownership, deterministic behavior coverage,
  provider retry parity, and inbound-reference closure are satisfied.
- Product requirements: autonomous profile answers remain concise, complete,
  and persisted in the same JSON shape; the banned DeepSeek regression is
  removed from this lane.
- Unsatisfied requirements found: none.

### Completion audit loop 2

- Re-read the migration handoff, completion plan, and predecessor audit after
  implementation; all now describe sole native ownership and Kimi retry order.
- Focused profile/planner tests passed 31/31. Web smoke passed 29 tests with 25
  environment-dependent skips. Pyright reported zero errors and compilation
  succeeded.
- A real GLM 5.2 response passed through the native default-dispatch seam with
  the explicit self-edit cwd override. No MiniMax or DeepSeek call ran.
- `mini-ork validate` passed. `mini-ork garden` returned zero errors and the
  pre-existing missing `docs/operator/env-vars.md` warning.
- Closure, diff, and provider-policy scans found no unsatisfied requirement.
