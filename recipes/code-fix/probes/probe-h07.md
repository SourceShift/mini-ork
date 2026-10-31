---
oracle: git
---
# Frozen probe probe-h07 — code-fix harvested task

FROZEN probe kickoff harvested by mini_ork.learning.probe_harvest from mini-ork's failure history. Never edit it to fit a candidate. It is graded by hidden tests, not by this text.

## Problem

fix(plan): repair invalid planner JSON via bounded LLM retry, not a hardcoded plan (#145)

When the planner LLM returned invalid JSON (or a plan missing
verifier_contract.checks), mini-ork silently substituted a deterministic
recipe plan built from workflow.yaml — losing model-authored verifier commands
and degrading planning quality.

Replace that degradation with a bounded LLM repair-retry loop, grounded in the
structured-output / self-repair literature:
- Iterative self-repair (arXiv:2604.10508) + error-reflection prompting
  (arXiv:2508.16729): re-invoke the planner with its raw invalid output + the
  EXACT validation error + an instruction to return ONLY valid JSON restating
  the required schema keys.
- Truncation guard (arXiv:2605.13076 / JSONSchemaBench 2501.10868): detect
  unbalanced-brace truncation (a top cause of invalid JSON) and flag it in the
  repair prompt.

Behavior:
- Recoverable validation failures (parse_error / missing_verifier_contract /
  bad_artifact_contract / bad_node_types) now trigger up to MO_PLAN_MAX_REPAIRS
  (default 2) LLM repair attempts, re-validated each time.
- If repairs are exhausted, planning HARD-FAILS (exit 1, D-015 forensics
  preserved) — Zero-Fallback, never a silent hardcoded plan.
- The old deterministic fallback survives only behind an explicit opt-in
  MO_PLAN_DETERMINISTIC_FALLBACK=1 (default OFF).

Applied identically to the bash CLI (bin/mini-ork-plan) and the Python port
(mini_ork/ported/mini_ork_plan.py) to keep the parity test green; adds tests
for repair-recovery, exhaustion hard-fail, and the deterministic opt-in.

Claude-Session: https://claude.ai/code/session_01MEQ3iCysDCxmTbvYa8PgMj

## Files in scope

- bin/mini-ork-plan
- mini_ork/ported/mini_ork_plan.py

## Definition of Done

Fix the behaviour described above so the repository state at HEAD satisfies the spec.
