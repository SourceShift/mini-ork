# I2b: the verifier audit must flag a textbook collapse (live smoke found it silent)

## Context

`mini_ork/learning/verifier_audit.py:audit(task_class, db)` (merged 4acb7cfe)
reads `collapse_history` and runs `hack_probe.monitor` and
`metric_anchor.audit`. A 2026-09-29 live smoke on a copy of the real state.db
injected 10 consecutive `collapse_history` rows for task_class `code_fix`:
score = 0.40 + 0.06*i (rising 0.40 → 0.94), anchor = 0.95 − 0.08*i (falling
0.95 → 0.23). The circuit breaker's collapse_detector tripped on exactly these
rows. The audit returned `ok=True, flags=[]`:

- metric_anchor: `n_generations=10` but `n_with_anchor=0` — the row mapping
  never fills the fields `metric_anchor.audit` reads, so it is inert.
- hack_probe: 10 generations with core; `level_gap p=0.754`,
  `change_point p=None`, `stagnation core_delta=-0.42 visible_delta=+0.30 p=None`,
  `confidently_wrong p=0.727` — no test decides.

## Mechanism (exact spec)

1. Fix the metric_anchor row mapping in `verifier_audit.py` so every
   collapse_history row carries the anchor fields `metric_anchor.audit`
   documents (read its module docstring and helpers `anchor_contaminated`,
   `under_anchored`); after the fix the injected fixture must report
   `n_with_anchor == 10`.
2. Add `collapse_detector.detect` as a third detector inside `audit` (it is the
   detector the breaker already trusts, over the same rows): a
   `recommendation == "halt"` adds flag `collapse`. Keep hack_probe and
   metric_anchor as they are (do NOT change their statistics); they contribute
   flags when they decide.
3. The evidence dict gains a `collapse` entry with the detector's report.

## Files in scope

- mini_ork/learning/verifier_audit.py
- tests/unit/test_verifier_audit_py.py

## Tests (add)

- the exact 10-row fixture above (seeded into a tmp sqlite `collapse_history`)
  → `ok is False` and `"collapse" in flags`; metric_anchor evidence shows
  `n_with_anchor == 10`;
- healthy rows (score and anchor both rise) → `ok is True`, no flags;
- fewer rows than the detectors' minimum → no flags.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_verifier_audit_py.py tests/unit/test_promotion_gate_py.py` exits 0. Run it and paste the last line.

## Rules

Edit files directly; do not emit unified diffs. Do not modify hack_probe.py,
metric_anchor.py, collapse_detector.py, or promotion_gate.py.
