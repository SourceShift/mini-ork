# I6 repair: pass-through before requiring confidence; keep the exact-set guard

## Context

The previous pass for `kickoffs/auto/rsi-i6-abstain-gate.md` is committed on this
branch (HEAD); read it — it is the spec. Two defects:

1. **Ordering bug.** In `mini_ork/gates/abstain_gate.py:_eval_abstain`, a
   verifier `pass` with no `confidence` (and no route margin) returns `defer`
   BEFORE the "fewer than `MO_ABSTAIN_MIN_CALIB` labelled rows → pass through
   unchanged" rule. The live DB has 0 `verifier_results` rows, so any recipe
   that opts in would defer every confidence-less pass with zero calibration.
   Spec rule 3 says no calibration → no abstention claims.
2. **Weakened guard.** `tests/unit/test_gate_evaluator_registry_py.py` was
   changed from an exact-set assertion over `GATE_EVALUATORS` to a superset
   check. The exact set exists to catch accidental registrations. Restore the
   exact-set assertion and add `"abstain_gate"` to the expected set.

## Mechanism (exact spec)

1. Reorder `_eval_abstain`: after the `fail`-stays-`fail` and verdict-validity
   checks, load the calibration scores FIRST; if there are fewer than
   `min_calib()` rows, return the verifier's verdict unchanged, regardless of
   whether a confidence value is present.
2. Only when calibration exists: a missing/invalid confidence → `defer`
   (current behaviour), otherwise the conformal comparison (current behaviour).

## Files in scope

- mini_ork/gates/abstain_gate.py
- tests/unit/test_abstain_gate.py
- tests/unit/test_gate_evaluator_registry_py.py

## Tests

Add to `tests/unit/test_abstain_gate.py`: with 0 calibration rows, a `pass`
with no confidence returns `pass` (not `defer`); with ≥30 rows, a `pass` with no
confidence returns `defer`.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_abstain_gate.py tests/unit/test_gate_evaluator_registry_py.py tests/unit/test_gate_registry_py.py` exits 0.
- Run it yourself and paste its last line into your final message.

## Rules

Edit files directly; do not emit unified diffs.
