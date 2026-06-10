# Planner Prompt

You are planning a read-only silent-catch audit for a TypeScript/JavaScript codebase.

Use the kickoff objective and any available run context to define:

- target repository or path
- included file globs
- excluded generated/vendor/test fixture paths
- allowlist comment phrases, defaulting to `non-blocking`, `fire-and-forget`, and `best effort`
- verdict threshold, defaulting to fail when any Critical finding exists

Do not propose source changes. Emit a short plan that the three audit lenses can follow.
