# I5 harness modules as apply-sweep targets (G02-T01, E6 slice)

## Goal

Harness adaptation around frozen models matched frontier results at ~90% lower
cost (arXiv 2607.08938). mini-ork's lanes are frozen APIs but its harness is
mutable, and `mini_ork/cli/apply.py:auto_sweep` only considers
`target LIKE 'agent.%'` (prompt directives). `harness_operator.py` (H1) already
proposes typed harness edits but applies nothing. This is the minimal slice of
`kickoffs/auto/rsi-e6-harness-evolution.md` that does not depend on unmerged E2/E4.

## Mechanism (exact spec)

1. Teach `auto_sweep` / `apply_run` a second target kind `harness.<recipe>.<node>`
   whose mutable surface is that node's prompt file under
   `recipes/<recipe>/prompts/` (resolved from the workflow's `prompt_ref`).
   Candidate mutations come from `harness_operator` proposals.
2. Scoring and gating reuse the exact existing apply path (probe_score +
   evaluate_gate + outcome-tagged edit memory). Add a cost term: record
   baseline vs candidate total probe cost; the decision log reports
   cost-per-solved-task for both arms. A candidate that keeps the solved set and
   lowers cost-per-solved-task is eligible; one that raises it is not.
3. Materialize candidates only in the temp recipe copy the apply loop already
   uses; never write into the live recipe during scoring.
4. Opt-in `MO_APPLY_HARNESS_TARGETS=1` (default OFF for this first slice).

## Files in scope

- mini_ork/cli/apply.py
- mini_ork/learning/harness_operator.py (only an adapter from proposal → mutation)
- tests/unit/test_apply_harness_targets.py (new)

## Tests

Hermetic `tests/unit/test_apply_harness_targets.py` with a stub scorer:
- with the flag off, auto_sweep's selected targets are unchanged;
- with the flag on, a harness candidate with equal solved set and lower cost is
  accepted; higher cost is rejected;
- the live recipe prompt file is byte-identical after scoring.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_apply_harness_targets.py tests/unit/test_cli_apply_py.py` passes.

## Rules

Edit files directly; do not emit unified diffs. Do not touch promotion_gate.py.
