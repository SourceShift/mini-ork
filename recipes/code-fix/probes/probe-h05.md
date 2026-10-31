---
oracle: git
---
# Frozen probe probe-h05 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(goal-loop): record real per-wave cost + classify hard-crash failures

The autoraise stop-gate counts only FUNDED waves (cost_usd >= $1), but
_default_run_wave_fn never set cost_usd -- it folded in panel-verdict.json (the
panel node's ~$0 cost), so every wave logged $0 and the predictor's
len(funded) < patience guard stayed true forever: the rail kept rising and the
loop never stopped on funded-but-flat progress. Measure real spend as the
today_cost_usd() delta across the wave subprocess (clamped >= 0 so a starved
wave or 24h-window slide reads 'not funded', never negative).

Make the fix-child kickoff failure-shape-adaptive: ch1's W9_scaffold_sections
now hard-fails (verified-artifact exit_code=1), a different shape than the
heading-drift the kickoff was written around and would mis-steer. Add a
classify-first step + the crash shape pointing the child at the real throw
chain (verifiedArtifactClient.ts -> verifiedArtifact{Production,DagRuntime}.ts,
both touched by the last compose commit) and telling it to reproduce for the
real stderr, since the DB last_error is truncated to ~220 chars.

Regression tests: funded delta recorded over panel's $0; non-positive clamp.

## Files in scope

- recipes/goal-loop/lib/drive.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
