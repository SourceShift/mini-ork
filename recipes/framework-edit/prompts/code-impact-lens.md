# Code Impact Lens Prompt

Read the planner output and the files likely to change.

Report:
- The smallest safe edit surface.
- Direct callers and tests likely affected.
- Blast-radius warnings, especially:
  - `lib/circuit_breaker.sh`
  - `lib/throttle-guard.sh`
  - `.mini-ork/config/**`
- Whether the requested change needs `scope_allow`.
- Suggested focused verification commands.

Keep the lens practical. Prefer file-path evidence over broad architectural
commentary.
