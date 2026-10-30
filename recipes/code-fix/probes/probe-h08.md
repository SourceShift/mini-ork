---
oracle: git
---
# Frozen probe probe-h08 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(self-improve): revert main-checkout leaks so a rejected iteration leaves main pristine

The self-improve loop edits a linked worktree that lives under the main tree
(.mini-ork/worktrees/iter-N-ts). The implementer harness has been observed
writing the patch back into the parent checkout instead of the worktree
(linked-worktree path resolution), so a rejected iteration could leave modified
tracked files and stray untracked files in main — iter-1 leaked verify.py +
test_verify.py that had to be hand-reverted.

Add a surface-scoped leak guard:
- _root_surface_dirty(root): NUL-delimited `git status --porcelain` over the
  improvable surface (mini_ork/ recipes/ schemas/ tests/), returning the set of
  currently-dirty paths.
- _restore_root_surface_leak(root, pre_dirty): reverts only paths that went
  clean->dirty during the run (checkout HEAD for tracked, remove for untracked);
  paths the operator already had dirty pre-run are never touched, so concurrent
  hand-edits are safe.

Snapshot pre_dirty before dispatch; restore after outcome is decided, before
`git worktree remove`, and log restored paths. Promotion still flows only through
the worktree branch + merge, never the main working tree.

[hidden]: builds a temp repo with an operator WIP file,
simulates a run leaking a tracked mutation + an untracked file + an
outside-surface change; asserts only the two in-surface leaks are reverted and
the WIP + outside-surface change are untouched. self_improve unit 7/7.

## Files in scope

- mini_ork/cli/self_improve.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
