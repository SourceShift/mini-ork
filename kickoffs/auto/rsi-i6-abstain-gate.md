# I6 calibrated abstain gate (G04-T05)

## Goal

`mini_ork/gates/gate_registry.py:gate_evaluate` already returns
`pass|fail|defer`, but no evaluator computes `defer` from calibrated
confidence, and `mini_ork/gates/oversight_inbox.py` is never enqueued. When a
verifier is unsure the loop must decline and escalate instead of issuing a
confident wrong verdict, keeping false completions at zero
(arXiv 2609.17686, 2609.28182).

## Mechanism (exact spec)

1. New module `mini_ork/gates/abstain_gate.py` registering gate type
   `abstain_gate` via `register_gate_evaluator`.
2. Input: the verifier result being gated plus a confidence score (from the
   verifier JSON field `confidence` if present, else from
   `mini_ork/dispatch/calibration.py`'s calibrated error estimate for the lane).
3. Calibration: a split-conformal threshold computed from the labelled history
   of past gate decisions in the DB (verifier verdict vs final outcome), at risk
   level `MO_ABSTAIN_ALPHA` (default 0.05). With fewer than
   `MO_ABSTAIN_MIN_CALIB` (default 30) labelled rows, the gate passes through
   the verifier's verdict unchanged (no calibration → no abstention claims).
4. Below threshold → return `defer` and enqueue an item via
   `oversight_inbox.enqueue` with the evidence; never convert a verifier `fail`
   into `pass`.
5. Opt-in per recipe by listing `abstain_gate` in gates (no recipe is changed
   in this task).

## Files in scope

- mini_ork/gates/abstain_gate.py (new)
- mini_ork/gates/gate_registry.py (registration hook only, if bootstrap requires it)
- tests/unit/test_abstain_gate.py (new)

## Tests

Hermetic `tests/unit/test_abstain_gate.py` (tmp sqlite):
- with ≥30 calibration rows, a low-confidence pass → `defer` + one inbox row;
- high-confidence pass → `pass`; any `fail` stays `fail`;
- <30 rows → verdict passes through unchanged;
- conformal threshold on a synthetic set achieves empirical risk ≤ alpha + tolerance.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_abstain_gate.py` plus the existing gate registry tests pass.

## Rules

Edit files directly; do not emit unified diffs. Do not wire the gate into any
recipe or the promotion gate in this task.
