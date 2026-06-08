# Production scenario: UI audit for first-run CLI/docs journey

## Surface

The first-run user journey across:

- README quickstart.
- `examples/00-demo.sh` output.
- `mini-ork help`.
- `mini-ork doctor`.
- Production scenario docs.

## Target users

- Engineers trying mini-ork for the first time.
- Maintainers validating a release candidate.
- Users who want to run from one `.md` file without learning internals first.

## Audit axes

- A11y/readability of command blocks and tables.
- Interaction clarity: can a user tell dry-run from live mode?
- Visual/scanning structure of README and docs.
- Edge cases: missing `yq`, no provider auth, no-Claude provider policy,
  ambiguous kickoff, oversized kickoff.

## Success criteria

- Findings include at least one recommendation for the `.md-only` dispatcher path.
- Findings include any confusing dry-run/live-mode wording.
- Findings cite exact file:line anchors.
- Findings completeness verifier passes.

## Risk tolerance

Read-only audit. Do not edit README during this run.
