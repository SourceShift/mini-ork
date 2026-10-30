---
oracle: git
holdout: true
---
# Frozen probe probe-h18 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(goal-loop): stop this run's env leaking into the deployed worker

The book-goal loop deploys the researcher worker with its own environment, and
that environment carries the goal-loop run's identity. The worker is long-lived
and re-mints a recipe per chapter, so inheriting any of it is wrong.

The live failure: MINI_ORK_PROJECT_HOME outranks MINI_ORK_HOME_DIR in
bin/mini-ork's launcher, so each per-chapter child wrote runs/<id>/
verified-artifact.json into the GOAL-LOOP's .mini-ork while verifiedArtifactClient
read the worktree's — ENOENT, "verifier did not run", for a run that verified
cleanly. Observed on W9_scaffold_sections: 7 attempts, mdlen=0, while chapters
2-10 held tens of KB of uncommitted markdown.

Scrub the run-scoped identity at the spawn source: the recipe/workflow/plan/
profile/node-input pins, the gate commands (pinned to `echo` upstream —
inheriting those would neuter every gate a child runs), the dispatch-chain lane
pin the dispatcher reads straight through as --model, and the home pair. Home
now travels with the replacement pin or not at all: all three home names are
re-pinned to the one resolved home, so no foreign value survives to split the
run dir from its reader.

Deliberately kept: ENGINE_ROOT/ROOT, so children run this loop's engine (main,
with the reviewer-diff and verifier-short-circuit fixes) rather than the
researcher home's older vendored copy; and DB, so the cost circuit keeps
reading the spend it caused.

## Files in scope

- kickoffs/book-goal-loop/binding/restart_worker.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
