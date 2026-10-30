---
oracle: git
---
# Frozen probe probe-h04 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(promote): require independent benchmark runs, not sample count

promotion_evaluate aggregated every benchmark_results row for a candidate
and never read run_id. One run emitting 30 passing rows was therefore
indistinguishable from 30 runs agreeing, so a single benchmark execution
could promote a candidate on 30 samples of one observation. Both the
samples and the all_pass flag came from the same run.

Independence is now measured separately from the sample count:
`_evidence_run_ids` returns the distinct run_id values behind a candidate,
and the decision tree rejects when that count is below
max(3, MO_PROMOTION_MIN_RUNS). The env var may raise the floor but never
lower it — a floor an environment can lower is not a floor.

A NULL/blank run_id is unproven independence and counts zero, and nothing
backfills a synthetic id: that would fabricate the exact independence being
demanded. The migrated schema declares run_id INTEGER NOT NULL so this
cannot arise there, but the guard holds for loose and legacy tables.

promotion_records.benchmark_run_id now records which runs the decision
rested on instead of a bare NULL, so the audit trail can answer the one
question a promotion has to answer. A multi-run decision has no single
benchmark_memory.summary_id to name, so the column carries the
comma-joined run list; application connections never enable FK
enforcement, and a promote resting on several runs is exactly the case the
column previously could not express.

Verified live through `mini-ork promote`: 30 passing rows all from run 1
give decision=rejected, rationale "insufficient independent evidence: 1
distinct run(s), 3 required; 30 sample(s) recorded", candidate left in
shadow with no version bump; the same 30 rows spread across runs 1-3
promote with n_runs=3.

Tests updated deliberately, not weakened: the all-pass seed helper now
spreads its rows across 3 distinct runs (four samples from one run can no
longer clear the floor however well they pass), and four new cases pin the
independence rule (single run rejected, floor cannot be lowered, floor can
be raised, NULL/blank run_id dropped).

## Files in scope

- mini_ork/gates/promotion_gate.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
