# I1 matched-attempt control arm for the apply gate (G06-T03)

## Goal

`mini_ork/learning/probe_scorer.py:probe_score` compares a candidate against a
SINGLE run of the unmutated recipe per probe task, and `mini_ork/cli/apply.py`
(`evaluate_gate`, `apply_run`) treats that one baseline as the truth. Run-to-run
noise in the unmutated recipe can therefore be credited to the candidate.
Evidence: blind resampling outperforms self-repair under placebo control
(arXiv 2607.26117); repaired-policy success is dominated by upstream variance
(arXiv 2607.17136). A candidate must beat the best of N blind retries, not one.

## Mechanism (exact spec)

1. New env knob `MO_APPLY_PROBE_CONTROL_N` (int, default 3, minimum 1; `1`
   reproduces today's behaviour exactly). In `probe_score` (and `probe_score_code`
   if it shares the baseline loop), run the unmutated baseline arm N times per
   probe task instead of once. Keep the existing budget caps
   (`MO_APPLY_PROBE_BUDGET_USD`, `MO_APPLY_PROBE_MAX_TASKS`) binding across all
   arms: stop and return `None` (unscored → quarantine) rather than overspend.
2. Per task, the control outcome is the BEST of the N baseline attempts
   (a task counts as solved by control if any retry solved it). The candidate
   gets credit for a task only if it solves a task the control did not.
3. Return the per-task control results alongside the existing fields
   (additive keys only; existing callers keep working), including
   `control_n` and `control_solved`.
4. `evaluate_gate` in `apply.py` uses the control-arm outcome as the baseline:
   reject (quarantine) a candidate whose solved set is not a strict superset gain
   over the control's solved set; the existing per-task no-regression rule still
   applies against the control.
5. Record `control_n` in whatever the gate already persists for the decision
   (log line + returned dict), so a promotion is auditable.

## Files in scope

- mini_ork/learning/probe_scorer.py
- mini_ork/cli/apply.py
- tests/unit/test_probe_scorer_control_arm.py (new)

## Tests

New `tests/unit/test_probe_scorer_control_arm.py`, hermetic (stub the per-arm
runner; no LLM, no network):
- with N=3 and a baseline that solves task T on 1 of 3 retries, a candidate that
  also solves T gets NO credit for T;
- a candidate that solves a task no retry solved is credited;
- `MO_APPLY_PROBE_CONTROL_N=1` yields byte-identical decisions to the pre-change
  path for the same stubbed outcomes;
- budget exhaustion mid-control returns `None` (unscored), never a score.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_probe_scorer_control_arm.py tests/unit/test_cli_apply_py.py tests/unit/test_probe_scorer_code_arm.py` passes.
- No new required env var; default behaviour changes only through N=3.

## Rules

Edit files directly; do not emit unified diffs. Do not touch promotion_gate.py,
execute.py, or any recipe.
