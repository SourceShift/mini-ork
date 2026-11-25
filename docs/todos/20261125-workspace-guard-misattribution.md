# Workspace creation failure is reported as "not a git repo"

Status: **open**
Opened: 2026-11-25
Found by: launching `mini-ork run framework-edit … --workspace worktree` for
`wu-memory-backend-seam`

## What happened

The run refused immediately with:

```
[workspace] review required but target is not a git repo: /Volumes/docker-ssd/ps/mini-ork
```

The target *is* a git repo (`git rev-parse --show-toplevel` succeeds there). The
real cause is the `reference-transaction` guard rejecting the branch that
`workspaces.create` asks for:

```
[worktree-guard] REJECT direct feature-branch creation: mini-ork/run-…
fatal: ref updates aborted by hook
```

`mini_ork/cli/main.py:1646` catches every `RuntimeError` from
`workspaces.create` and prints the single "not a git repo" message, so a guard
rejection, a lockfile, a disk-full, or a genuinely missing repo are
indistinguishable. Diagnosing it took a manual `git branch` probe; an operator
would reasonably conclude the checkout is corrupt.

The guard itself prints the fix (`ALLOW_WORKTREE_BRANCH_CREATE=1`, or
`scripts/mini_ork_worktree.py create`), but that text is swallowed.

## Suggested fix

`workspaces.create` already raises distinct messages (`not a git repo: …`,
`worktree add failed: …`, `could not read HEAD: …`). Surface the exception text
instead of replacing it, keeping the fail-closed prefix:

```
[workspace] review required but could not isolate the run: <exception text>
```

and add a branch for a guard rejection that names the bypass, since that is the
common case on this checkout.

## Why it matters

Every self-edit run on this repo needs `--workspace worktree`, and the guard is
active on every clone with `.githooks` installed. This is the first thing a new
operator hits, and the message sends them to the wrong subsystem.

## Test

`tests/unit/test_workspace_failure_message_py.py` — patch `workspaces.create` to
raise each of the three `RuntimeError` shapes and assert the printed line keeps
the exception text; assert the guard-rejection case names
`ALLOW_WORKTREE_BRANCH_CREATE=1`.
