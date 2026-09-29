# I2 verifier validity audit as a promotion precondition (G03-T08)

## Goal

Three shipped measurement modules each detect a way the grading signal lies,
but nothing acts on them: `mini_ork/gates/gate_fuzzer.py` (G3 blind-spot /
over-block fuzzing), `mini_ork/learning/hack_probe.py` (H2 reward-hacking
monitor, arXiv 2609.04665), `mini_ork/learning/metric_anchor.py` (H5
score/anchor divergence, arXiv 2607.12790). Phantom self-training gains are
exposed only by frozen-control audits (arXiv 2608.20290). The promotion path
must refuse to trust a grader that its own audits flag.

## Mechanism (exact spec)

1. New module `mini_ork/learning/verifier_audit.py` with
   `audit(task_class, db_path, ...) -> dict` that runs the three existing
   detectors in-process (reusing their public functions; do not reimplement
   them) and returns `{"ok": bool, "flags": [...], "evidence": {...}}`. A detector
   with insufficient data contributes no flag (fail-open per detector, logged).
2. In `mini_ork/gates/promotion_gate.py:promotion_evaluate`, before a promote
   decision, call `verifier_audit.audit`; if `ok` is false, the decision becomes
   `quarantined` with reason `verifier-audit:<flags>`. Never turn a reject into
   a promote.
3. Opt-out `MO_PROMOTION_VERIFIER_AUDIT=0` (default ON).

## Files in scope

- mini_ork/learning/verifier_audit.py (new)
- mini_ork/gates/promotion_gate.py
- tests/unit/test_verifier_audit.py (new)

## Tests

Hermetic `tests/unit/test_verifier_audit.py`:
- a hack-probe flag in seeded data → promotion quarantined with reason;
- clean data → promotion decision unchanged;
- detectors with no data → no flags;
- opt-out restores prior behaviour.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_verifier_audit.py` plus the existing promotion gate tests pass.

## Rules

Edit files directly; do not emit unified diffs. Do not touch apply.py.
