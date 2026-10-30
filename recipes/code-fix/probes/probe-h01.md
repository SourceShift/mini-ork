---
oracle: git
---
# Frozen probe probe-h01 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(reviewer): relativize declared paths so the diff is never silently empty

The reviewer read a 0-byte review-diff.patch and passed a run whose edit it
never saw. Three defects combined to produce that:

1. files_changed is recorded ABSOLUTE (publisher's commit gate and
   _revert_branch both need that shape), but git accepts a pathspec only
   relative to the repo root. An absolute path matched NOTHING and exited 0,
   so the diff was empty and the reviewer was handed "(no diff)".
2. An untracked symlink (baml_client -> a neighbouring checkout) resolved
   outside the worktree via realpath, recording a path that can never be a
   pathspec at all.
3. Nothing distinguished "the implementer changed nothing" from "the capture
   failed", so both read as a clean pass.

_review_pathspecs relativizes the declared entries, drops any that resolve
outside the repo, and drops pathspec magic. _assemble_reviewer_inputs now
widens rather than narrows: when no pathspec survives it captures the whole
delta, since a wider diff can never hide a real edit. When the declared files
are demonstrably unchanged in both the tracked diff and the untracked list,
review-diff-noop.json is written and _handle_reviewer blocks with rc 1 before
any model spend. An untracked new file is real work and is explicitly not a
no-op.

The sweep entry also carried no child_run_id, so the outer loop could not
attribute a wave's outcome to the child that produced it. SpawnResult now
exposes child_run_id/child_run_dir and the driver records child_verdict,
child_failed_nodes, review_diff_bytes and child_no_op alongside it.

Carry-forward: the verifier short-circuit fix (a declared verifier_ref must
run even when artifact_contract.outputs is empty) was sitting uncommitted in
the main checkout; it is included here so the fix is not lost.

## Files in scope

- mini_ork/cli/execute.py
- mini_ork/cli/execute_handlers.py
- mini_ork/cli/spawn.py
- recipes/goal-loop/lib/drive.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
