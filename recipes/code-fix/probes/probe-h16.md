---
oracle: git
holdout: true
---
# Frozen probe probe-h16 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(publisher): keep bare-relative outputs run-local, not repo-root commits

A bare relative artifact_contract output (no directory component) -- e.g.
goal-loop's panel-verdict.json -- was joined against the mini-ork repo root
and then git-committed there, landing a stray "audit(<recipe>): publish
synthesis from <run>" commit on the repo root on every wave. The run_local
guard could never catch it: dst was already resolved against root, so a bare
name never satisfied the run_root prefix test.

Resolve a bare-name output against run_dir (its canonical producer-written
location) so it is preserved in place -- never copied or committed to root.
Relative paths WITH a directory component (docs/refactor/foo.md) stay
repo-relative publish targets; absolute ${MINI_ORK_RUN_DIR}/foo paths are
unchanged. Same latent leak is fixed for blog-cohesion, book-gen-flow-audit,
and chapter-review bare-name outputs.

Regression test asserts the verdict never lands at the repo root and HEAD does
not advance; verified to fail on the pre-fix publisher.

## Files in scope

- mini_ork/cli/publisher.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
