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

<!-- applied:gradient_records:gr-7b8e0368cc24 -->
- Observation: The lens produced an artifact without reading any files or using tools, despite the task class being framework_edit and the objective domain being code-delivery. That makes the code-impact assessment likely generic or weakly grounded.
- Directive: Require the code-impact lens prompt to inspect the relevant changed files, adjacent framework files, and at least one project convention source before writing its lens artifact. Add an explicit evidence section with file paths and concrete code anchors.
