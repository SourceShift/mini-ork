---
oracle: git
---
# Frozen probe probe-h10 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(goal-loop): fail-fast on worker-exhausted-idle, not just generating-stall

The #36 terminal-failure detector only tripped on status=generating with an
idle run-dir (orphaned mid-generation). Live monitoring of book d0df3cdb showed
the DOMINANT stall is different: the worker burns its per-dispatch retry budget
(generation_attempts climbs to 6 = two cycles of 3), leaves the chapter failed
(NOT permanently_failed), and goes idle. The old detector missed this, so
drive.py await sat out the full 90-min MO_GOAL_APPLY_AWAIT_SECONDS window on a
chapter only a redispatch could advance.

Broaden the stall set to _STALL_STATUSES = {generating, failed, error} and keep
the idle-run-dir gate as the safe discriminator: a chapter racily failed BETWEEN
retries still has a churning worker (fresh run-*), so age < stall => NOT
terminal; only a genuinely stopped worker (age >= stall) trips it. This is
strictly safer than MO_GOAL_FAIL_ON_STATUS_FAILED, which fires on failed
immediately and would abandon a chapter mid-retry.

3 new hermetic tests mock the psql wrapper _q and stamp run-dir mtimes:
worker-exhausted-idle=terminal, failed-but-churning=not-terminal,
permfail=always-terminal. 33 pass.

## Files in scope

- kickoffs/book-goal-loop/binding/chapter_terminal_fail.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
