# I6b: abstain_gate must defer MORE when the verifier is unreliable, not never

## Context

`mini_ork/gates/abstain_gate.py` (merged f3d933c4) computes a split-conformal
threshold as the (1−alpha) quantile of non-conformity scores, where a wrong
labelled row scores 1.0 and a correct pass scores `1 − confidence`. A
2026-09-29 live smoke on a copy of the real state.db showed:

- 40 labelled `verifier_results` rows (verifier `test`), 1 wrong (2.5%):
  threshold 0.4 → passes with confidence 0.05/0.3/0.55 defer, 0.7/0.99 pass,
  3 oversight-inbox rows. Correct.
- The same 40 rows with 4 wrong (10%, above alpha=0.05): the quantile lands on
  a 1.0 score, the threshold saturates at 1.0, and a confidence-0.05 pass
  returns `pass`. The gate stops abstaining exactly when the verifier is least
  trustworthy. That is the opposite of the spec's purpose (false completions
  held at zero).

## Mechanism (exact spec)

1. Replace the quantile rule with a risk-controlling confidence threshold
   (Learn-Then-Test style, over the same labelled rows): choose the smallest
   confidence threshold t such that, among labelled pass rows with
   confidence ≥ t, the empirical false-positive rate — with a finite-sample
   upper bound, e.g. a one-sided Clopper-Pearson or (k+1)/(n+1) bound — is
   ≤ alpha. A pass with confidence ≥ t passes; below t → defer + enqueue.
2. If no threshold achieves the bound (the verifier is too unreliable at every
   confidence), EVERY pass defers (and enqueues), with reason
   `verifier-uncertifiable-at-alpha`.
3. Keep: fail stays fail; fewer than `MO_ABSTAIN_MIN_CALIB` labelled rows →
   pass through unchanged; missing confidence with calibration → defer.

## Files in scope

- mini_ork/gates/abstain_gate.py
- tests/unit/test_abstain_gate.py

## Tests (add)

- 40 rows, 1 wrong at low confidence → low-confidence pass defers,
  high-confidence pass passes;
- 40 rows, 4 wrong spread across confidences (10% > alpha) → a 0.99-confidence
  pass DEFERS with reason `verifier-uncertifiable-at-alpha` (fails on current code);
- on a synthetic set, the empirical false-positive rate among accepted passes
  is ≤ alpha.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_abstain_gate.py tests/unit/test_gate_evaluator_registry_py.py tests/unit/test_gate_registry_py.py` exits 0. Run it and paste the last line.

## Rules

Edit files directly; do not emit unified diffs.
