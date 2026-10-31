---
oracle: git
holdout: true
---
# Frozen probe probe-h19 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(goal-loop): give the deploy resync a git identity

The restart binding's `_sync_upstream` merges the product branch into the
deploy target with `git merge --no-edit`. A merge writes a commit, so on a
runner with no configured identity git dies with `empty ident name ... not
allowed` — the state every CI runner and fresh container starts in. The
binding reports any failed merge as `merge conflict against main`, so the
deploy target silently stopped carrying the product branch forward, which is
exactly the regression this binding exists to prevent: the book worker kept
running a branch 14 commits behind while the fix it needed was reported as
shipped.

Supply the project-wide fallback identity (`mini-ork` / `mini-ork@localhost`,
matching `vcs/rebase_guard.py` and `vcs/auto_merge.py`) and apply it only to
the mutating merge call, leaving other git invocations on the inherited env.
A configured identity still wins, and an explicitly empty one is filled too —
that empty-variable case is what CI actually presents.

The new test empties the four `GIT_*` variables and points both config files
at the null device so git has no name to write, which reproduces the failure
on macOS as well as Linux; without the fix it reports the same
`merge conflict against main (fatal: empty ident name ...)` CI saw.

## Files in scope

- kickoffs/book-goal-loop/binding/restart_worker.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
