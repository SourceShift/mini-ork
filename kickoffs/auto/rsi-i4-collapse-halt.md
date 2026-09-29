# I4 collapse halt: wire collapse_detector into the circuit breaker (G01-T04)

## Goal

`mini_ork/learning/collapse_detector.py:detect` (G7) returns
`recommendation: "halt"` but only `cli/collapse.py` calls it and always exits 0.
`mini_ork/recovery/circuit_breaker.py` is live (reached via the registered
`liveness_gate`) but trips only on artifact-hash, verdict, and cost stagnation.
Self-training that rises then collapses is the measured default
(arXiv 2606.21090), so the measurement must be able to stop the loop.

## Mechanism (exact spec)

1. Add a fourth breaker signal in `circuit_breaker.py`, `_eval_collapse_signal`,
   alongside `_eval_artifact_signal` / `_eval_verdict_signal` / `_eval_cost_signal`.
   It calls `collapse_detector.detect` with the same inputs `cli/collapse.py`
   builds, and reports a trip when `recommendation == "halt"`.
2. Opt-out `MO_CB_COLLAPSE=0` (default ON). Any exception or insufficient data
   inside the collapse signal is NOT a trip (fail-open for liveness, but log the
   reason once) — a broken detector must not halt healthy runs.
3. The trip reason string names the collapse evidence (the detector's own
   summary fields) so `circuit_breaker_state` rows are auditable.
4. Do not modify `collapse_detector.py`'s logic; if it needs a small pure helper
   to be callable in-process, add it there without changing `detect` semantics.

## Files in scope

- mini_ork/recovery/circuit_breaker.py
- mini_ork/learning/collapse_detector.py (only if a pure helper is needed)
- tests/unit/test_circuit_breaker_collapse.py (new)

## Tests

New hermetic `tests/unit/test_circuit_breaker_collapse.py` (tmp sqlite DB):
- a synthetic score-rises/anchor-falls history → breaker trips with a
  collapse reason;
- a healthy history → no trip;
- `MO_CB_COLLAPSE=0` → never trips on collapse;
- detector raising / empty history → no trip.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_circuit_breaker_collapse.py tests/unit/test_circuit_breaker_py.py tests/unit/test_collapse_detector_py.py` passes.

## Rules

Edit files directly; do not emit unified diffs. Do not touch apply.py,
promotion_gate.py, or execute.py.
