# Example Kickoff

Audit this repository for silent catch anti-patterns in TypeScript and JavaScript.

Scope:

- Include `src/**/*.ts`, `src/**/*.tsx`, `app/**/*.ts`, `app/**/*.tsx`, `lib/**/*.js`, and `scripts/**/*.js`
- Exclude generated files, vendored files, fixtures, snapshots, and build output
- Treat comments containing `non-blocking`, `fire-and-forget`, or `best effort` within two lines of a catch site as possible allowlist evidence

Success criteria:

- Produce `silent-catch-audit.md`
- Produce `silent-catch-audit.findings.json`
- Return `fail` if any Critical silent catch exists
- Keep the audit read-only: do not edit source files and do not add ESLint config

Critical examples:

- swallowed advisory lock release failure
- swallowed transaction commit or rollback failure
- swallowed queue acknowledgement failure
- swallowed auth, billing, or migration failure
