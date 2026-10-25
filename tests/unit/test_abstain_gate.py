"""Unit tests for ``mini_ork.gates.abstain_gate`` — the Learn-Then-Test bar.

The module used to register an ``abstain_gate`` gate evaluator; that was
removed in 2026-10 (docs/plans/2026-10-25-ecosystem-section6.md). What stays,
and what these tests pin, is the LTT confidence bar the self-driving escalation
policy calibrates with (``mini_ork/recovery/escalation.py``) and its one-way
env clamps.
"""

from __future__ import annotations

import random

from mini_ork.gates import abstain_gate
from mini_ork.gates import gate_registry as gr
from mini_ork.gates.abstain_gate import _ltt_min_confidence


def test_module_no_longer_registers_a_gate_evaluator():
    from mini_ork.gates import gate_bootstrap  # noqa: F401  (boot the registry)

    assert "abstain_gate" not in gr.GATE_EVALUATORS
    assert "conformal_gate" not in gr.GATE_EVALUATORS
    assert not hasattr(abstain_gate, "_eval_abstain")


def test_empty_labelled_set_is_the_permissive_fallback():
    assert _ltt_min_confidence([], 0.05) == (0.0, False, 0, 0, 0.0)


def test_all_correct_rows_certify_at_the_lowest_confidence():
    rows = [(c / 100, False) for c in range(50, 100)]
    bar, uncertifiable, accepted_n, accepted_wrong, bound = _ltt_min_confidence(rows, 0.05)
    assert not uncertifiable
    assert bar == 0.5
    assert (accepted_n, accepted_wrong) == (50, 0)
    assert bound == 1 / 51


def test_a_noisy_verifier_is_uncertifiable():
    rows = [(0.9, True)] * 10 + [(0.8, False)] * 10
    bar, uncertifiable, accepted_n, accepted_wrong, bound = _ltt_min_confidence(rows, 0.05)
    assert uncertifiable
    assert bar == 0.0
    assert (accepted_n, accepted_wrong) == (20, 10)
    assert bound == 11 / 21


def test_alpha_env_may_only_tighten(monkeypatch):
    monkeypatch.setenv("MO_ABSTAIN_ALPHA", "0.5")
    assert abstain_gate.alpha() == abstain_gate.DEFAULT_ALPHA
    monkeypatch.setenv("MO_ABSTAIN_ALPHA", "0.01")
    assert abstain_gate.alpha() == 0.01
    monkeypatch.setenv("MO_ABSTAIN_ALPHA", "junk")
    assert abstain_gate.alpha() == abstain_gate.DEFAULT_ALPHA


def test_min_calib_env_may_only_raise(monkeypatch):
    monkeypatch.setenv("MO_ABSTAIN_MIN_CALIB", "3")
    assert abstain_gate.min_calib() == abstain_gate.DEFAULT_MIN_CALIB
    monkeypatch.setenv("MO_ABSTAIN_MIN_CALIB", "80")
    assert abstain_gate.min_calib() == 80


def test_10_ltt_empirical_fpr_within_alpha():
    """LTT scenario C: on a synthetic held-out set drawn from the
    same distribution as the calibration, the empirical FPR among
    accepted rows is ≤ alpha + tolerance.

    The (k+1)/(n+1) bound is a high-confidence upper bound on the
    true FPR — with enough held-out trials the empirical FPR should
    stay within alpha of the true FPR. We seed calibration as 95%
    correct at high confidence + 5% wrong at low confidence; the LTT
    algorithm picks the smallest confidence bar such that
    ``(k+1)/(n+1) ≤ alpha`` over accepted rows, which gives an
    accepted set whose empirical FPR is bounded by alpha.
    """
    rng = random.Random(42)
    alpha_val = 0.10
    tolerance = 4e-2  # generous for held-out noise (sqrt(0.05*0.95/10000)≈2e-3)

    # Calibration: 190 correct at conf uniform in [0.6, 1.0], 10 wrong
    # at conf uniform in [0.05, 0.25]. Wrong rows are well below the
    # bulk of correct rows, so the LTT threshold sits near the wrong
    # rows' confidence — wrong rows are accepted at the smallest t
    # that still satisfies the bound.
    correct = [(rng.uniform(0.6, 1.0), False) for _ in range(190)]
    wrong = [(rng.uniform(0.05, 0.25), True) for _ in range(10)]
    lab = correct + wrong

    min_conf, uncert, accepted_n, accepted_wrong, bound = _ltt_min_confidence(
        lab, alpha_val
    )

    assert not uncert, "calibration should be certifiable at alpha=0.10"
    assert bound <= alpha_val, (
        f"LTT bound {bound} > alpha {alpha_val} (algorithm violation)"
    )
    # Accepted set covers every row at the smallest t (since including
    # wrong rows keeps the bound at (k+1)/(n+1) ≤ alpha).
    assert accepted_n == len(lab), (
        f"smallest t should accept the full set; got accepted_n="
        f"{accepted_n} of {len(lab)}"
    )
    assert accepted_wrong == len(wrong), (
        f"smallest t should accept all wrong rows when bound ≤ alpha; "
        f"got accepted_wrong={accepted_wrong}"
    )

    # Held-out: same distribution. With 10 000 trials the empirical
    # FPR is well within a few percent of the true FPR (≈ 5%).
    rng_h = random.Random(43)
    held_correct = [(rng_h.uniform(0.6, 1.0), False) for _ in range(9500)]
    held_wrong = [(rng_h.uniform(0.05, 0.25), True) for _ in range(500)]
    held = held_correct + held_wrong

    accepted = [(c, w) for c, w in held if c >= min_conf]
    assert accepted, "held-out set should produce accepted rows"
    empirical_fpr = sum(1 for _, w in accepted if w) / len(accepted)

    assert empirical_fpr <= alpha_val + tolerance, (
        f"empirical FPR {empirical_fpr:.4f} exceeds alpha + tolerance "
        f"({alpha_val} + {tolerance}); LTT threshold did not bound"
    )
