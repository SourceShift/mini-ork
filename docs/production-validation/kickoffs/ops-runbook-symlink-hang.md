# Production scenario: ops runbook for symlink security-test hang

## Incident

`tests/run-all.sh` can hang in `tests/security/test_sec_symlink_attacks.sh`
while SQLite opens `.mini-ork/state.db` when it is a symlink to `/etc/passwd`.

## Target operator

Maintainer running release validation on macOS or Linux.

## Desired runbook

- Detect the hung process tree.
- Confirm whether `/etc/passwd` content changed.
- Stop only the affected test processes.
- Clean temporary `.mini-ork` directories safely.
- Decide whether to rerun the suite, skip the security test, or patch
  `db/init.sh` with explicit symlink rejection.

## Success criteria

- Runbook includes commands for `pgrep`, `ps`, `stat`, and cleanup.
- Runbook distinguishes data-corruption risk from test-harness hang.
- Prevention section recommends a timeout or explicit `[ -L "$DB" ]` guard.
- Runbook verifier passes.

## Risk tolerance

Medium. This is operational guidance; no code changes during the run.
