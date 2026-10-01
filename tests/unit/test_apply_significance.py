"""Tests for the opt-in McNemar significance gate inside
`mini_ork.cli.apply.evaluate_gate`.

The new helper `mcnemar_exact_p(gains, losses)` is the pure math primitive
(kickoff step 1); `evaluate_gate` consumes it on the measured-promoted path
only (kickoff step 4). Enforcement is off by default — `MO_APPLY_SIG_ALPHA`
unset keeps the decision identical to pre-change behaviour apart from the
additive `sig_*` keys.

These tests pin:
  1. the exact binomial table for the helper (powers-of-2 denominators
     give exact floats, so the table is reproducible to the bit)
  2. the enforcement-OFF promote path (decision byte-identical, suffix lands)
  3. the enforcement-ON insufficient-evidence quarantine
  4. the enforcement-ON sufficient-evidence promote
  5. per-task regression still wins first (order-of-branches invariant)
  6. scalar path untouched when enforcement is on
  7. invalid `MO_APPLY_SIG_ALPHA` -> treated as unset, `sig_alpha_error`
     recorded
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.cli import apply as ap


@pytest.fixture(autouse=True)
def _scrub_sig_alpha(monkeypatch):
    """The suite-wide conftest autouse restores ``os.environ`` per test, but
    we still scrub ``MO_APPLY_SIG_ALPHA`` explicitly so the outer pytest
    process's env can't bleed into a test that intends enforcement off (and
    vice versa). ``monkeypatch`` restores on teardown."""
    monkeypatch.delenv("MO_APPLY_SIG_ALPHA", raising=False)
    return monkeypatch


# ── 1. mcnemar_exact_p numeric table ────────────────────────────────────────

def test_mcnemar_exact_p_degenerate_edges():
    assert ap.mcnemar_exact_p(0, 0) == 1.0
    # One-sided exact, so gains==0 with any losses still has p==1.0 only
    # when n==0; otherwise it is the chance of zero successes in n flips.
    assert ap.mcnemar_exact_p(1, 0) == 0.5
    assert ap.mcnemar_exact_p(4, 0) == 0.0625
    assert ap.mcnemar_exact_p(5, 0) == 0.03125
    assert ap.mcnemar_exact_p(3, 1) == 0.3125


def test_mcnemar_exact_p_monotone_in_gains_at_fixed_n():
    # More gains at fixed n -> smaller upper-tail p.
    n = 6
    ps = [ap.mcnemar_exact_p(g, n - g) for g in range(n + 1)]
    assert ps == sorted(ps, reverse=True), (
        f"expected monotone decreasing in gains, got {ps}")
    # Edge checks: all-fail (g=0) -> 1.0; all-gain (g=n) -> 1/2**n.
    assert ps[0] == 1.0
    assert ps[-1] == 1 / (2 ** n)


# ── 2. enforcement OFF: promote gains suffix, gains/losses/p keys emitted ──

def test_evaluate_gate_off_records_sig_keys_and_suffix():
    out = json.loads(ap.evaluate_gate(
        "cand-test", 0.5, 0.7, '{"before":[0,0],"after":[1,0]}'))
    assert out["decision"] == "promoted"
    assert out["sig_p"] == 0.5
    assert out["sig_gains"] == 1
    assert out["sig_losses"] == 0
    assert out["rationale"].endswith("McNemar p=0.5000")


# ── 3. enforcement ON insufficient -> quarantined with the exact template ──

def test_evaluate_gate_on_insufficient_quarantines(_scrub_sig_alpha):
    monkeypatch = _scrub_sig_alpha
    monkeypatch.setenv("MO_APPLY_SIG_ALPHA", "0.1")
    out = json.loads(ap.evaluate_gate(
        "cand-test", 0.5, 0.7, '{"before":[0,0],"after":[1,0]}'))
    assert out["decision"] == "quarantined"
    assert out["rationale"].startswith("insufficient evidence")
    assert out["sig_alpha"] == 0.1
    assert out["sig_p"] == 0.5
    assert out["sig_gains"] == 1
    assert out["sig_losses"] == 0


# ── 4. enforcement ON sufficient -> promoted with McNemar suffix ───────────

def test_evaluate_gate_on_sufficient_promotes(_scrub_sig_alpha):
    monkeypatch = _scrub_sig_alpha
    monkeypatch.setenv("MO_APPLY_SIG_ALPHA", "0.1")
    # 5 gains, 0 losses over 6 probes: p = 1/32 = 0.03125 <= 0.1.
    out = json.loads(ap.evaluate_gate(
        "cand-test", 0.5, 0.7,
        '{"before":[0,0,0,0,0,0],"after":[1,1,1,1,1,0]}'))
    assert out["decision"] == "promoted"
    assert out["rationale"].endswith("McNemar p=0.0312")
    assert out["sig_alpha"] == 0.1
    assert out["sig_p"] == 0.03125
    assert out["sig_gains"] == 5
    assert out["sig_losses"] == 0


# ── 5. per-task regression still wins first (kickoff step 6 ordering) ──────

def test_evaluate_gate_on_regression_wins_before_significance(_scrub_sig_alpha):
    monkeypatch = _scrub_sig_alpha
    monkeypatch.setenv("MO_APPLY_SIG_ALPHA", "0.1")
    # aggregate up, but one previously-solved task regressed; gains (1)
    # would clear significance (p=0.5 > 0.1 would actually quarantine,
    # but per-task regression fires first with the no-regression rationale).
    out = json.loads(ap.evaluate_gate(
        "cand-test", 0.5, 0.7, '{"before":[1,1,1],"after":[1,0,1]}'))
    assert out["decision"] == "quarantined"
    assert out["rationale"].startswith("per-task no-regression gate")
    assert "insufficient evidence" not in out["rationale"]


# ── 6. scalar path untouched when enforcement is on ────────────────────────

def test_evaluate_gate_scalar_path_ignores_significance(_scrub_sig_alpha):
    monkeypatch = _scrub_sig_alpha
    monkeypatch.setenv("MO_APPLY_SIG_ALPHA", "0.1")
    # No pertask_json -> measured=False -> significance is a no-op:
    # decision unchanged, sig_p is None, gains/losses omitted. The
    # configured alpha is still reported so the operator can confirm the
    # gate is armed even on the scalar path.
    promoted = json.loads(ap.evaluate_gate("cand-test", 0.5, 0.7))
    assert promoted["decision"] == "promoted"
    assert promoted["sig_p"] is None
    assert "sig_gains" not in promoted
    assert "sig_losses" not in promoted
    assert promoted["sig_alpha"] == 0.1

    reg = json.loads(ap.evaluate_gate("cand-test", 0.7, 0.5))
    assert reg["decision"] == "quarantined"
    assert reg["sig_p"] is None
    assert reg["sig_alpha"] == 0.1


# ── 7. invalid alpha -> treated as unset, sig_alpha_error recorded ─────────

def test_evaluate_gate_invalid_alpha_treated_as_unset(_scrub_sig_alpha):
    monkeypatch = _scrub_sig_alpha
    monkeypatch.setenv("MO_APPLY_SIG_ALPHA", "banana")
    out = json.loads(ap.evaluate_gate(
        "cand-test", 0.5, 0.7, '{"before":[0,0],"after":[1,0]}'))
    # Decision still promotes (enforcement off), suffix still lands.
    assert out["decision"] == "promoted"
    assert out["sig_alpha_error"] == "banana"
    assert "sig_alpha" not in out
    assert out["rationale"].endswith("McNemar p=0.5000")


# ── 8. boundary alpha values: 0 and 1 -> out-of-range -> treated as unset ──

@pytest.mark.parametrize("bad_alpha", ["0", "0.0", "1.5", "-0.1"])
def test_evaluate_gate_out_of_range_alpha_treated_as_unset(_scrub_sig_alpha,
                                                            bad_alpha):
    monkeypatch = _scrub_sig_alpha
    monkeypatch.setenv("MO_APPLY_SIG_ALPHA", bad_alpha)
    out = json.loads(ap.evaluate_gate(
        "cand-test", 0.5, 0.7, '{"before":[0,0],"after":[1,0]}'))
    assert out["decision"] == "promoted"
    assert out["sig_alpha_error"] == bad_alpha
    assert "sig_alpha" not in out