"""Unit tests for ``mini_ork.learning.utility_function.score``.

Asserts the documented default formula against hand-computed expectations:

    U = w_success*success + w_verifier*verifier_score + w_quality*quality_score
        - w_cost*norm_cost - w_latency*norm_latency - w_risk*risk_penalty

with all components clamped to [0, 1], U clamped to [0, 1], and default
weights success=0.45, verifier=0.20, quality=0.15, cost=0.10, latency=0.05,
risk=0.05 (overridable via the ``weights`` dict or ``MINI_ORK_W_*`` env).

The per-task override path (``${MINI_ORK_HOME}/config/utility_functions/
<task_class>.sh``) is impure I/O and intentionally not ported; every fixture
here omits ``task_class`` so the default formula branch runs.
"""

from __future__ import annotations

import math

import pytest

from mini_ork.learning.utility_function import score


@pytest.fixture(autouse=True)
def _clean_weight_env(monkeypatch):
    """Pin the default weights regardless of the developer's shell env."""
    for k in ("SUCCESS", "VERIFIER", "QUALITY", "COST", "LATENCY", "RISK"):
        monkeypatch.delenv(f"MINI_ORK_W_{k}", raising=False)


def test_f01_success_true_with_full_fields():
    """success=True + verifier + quality + cost + duration + risk — the meaty fixture."""
    run_json = (
        '{"success": true, "verifier_score": 0.8, "quality_score": 0.7,'
        ' "cost_usd": 0.5, "max_cost_usd": 1.0, "duration_ms": 5000,'
        ' "max_duration_ms": 10000, "risk_penalty": 0.1}'
    )
    # 0.45*1 + 0.20*0.8 + 0.15*0.7 - 0.10*0.5 - 0.05*0.5 - 0.05*0.1 = 0.635
    assert math.isclose(score(run_json), 0.635, abs_tol=1e-6)


def test_f02_success_false_with_quality_default():
    """success=False; verifier_score/risk_penalty default to 0; quality_score defaults to 0.5."""
    # 0.15 * 0.5 = 0.075
    assert math.isclose(score('{"success": false}'), 0.075, abs_tol=1e-6)


def test_f03_verifier_score_clamped_to_one():
    """verifier_score > 1 must clamp to 1.0 before weighting."""
    # 0.45*1 + 0.20*1.0 (clamped from 1.7) + 0.15*0.5 = 0.725
    assert math.isclose(score('{"success": true, "verifier_score": 1.7}'), 0.725, abs_tol=1e-6)


def test_f04_custom_max_cost_usd_normalization():
    """Custom ceiling changes norm_cost denominator."""
    # 0.45 + 0.15*0.5 - 0.10*(0.25/0.5) = 0.475
    assert math.isclose(
        score('{"success": true, "cost_usd": 0.25, "max_cost_usd": 0.5}'),
        0.475, abs_tol=1e-6)


def test_f05_custom_max_duration_ms_normalization():
    """Custom ceiling changes norm_latency denominator."""
    # 0.45 + 0.15*0.5 - 0.05*(3000/6000) = 0.5
    assert math.isclose(
        score('{"success": true, "duration_ms": 3000, "max_duration_ms": 6000}'),
        0.5, abs_tol=1e-6)


def test_f06_weight_override_via_weights_dict_and_env(monkeypatch):
    """The ``weights`` parameter and the ``MINI_ORK_W_*`` env vars are two
    names for the same override channel — both must yield the same score."""
    run_json = '{"success": true, "verifier_score": 0.5, "quality_score": 0.5}'
    weights = {
        "success": 0.30,
        "verifier": 0.30,
        "quality": 0.30,
        "cost": 0.03,
        "latency": 0.03,
        "risk": 0.04,
    }
    # 0.30*1 + 0.30*0.5 + 0.30*0.5 = 0.60
    assert math.isclose(score(run_json, weights=weights), 0.60, abs_tol=1e-6)
    for k, v in weights.items():
        monkeypatch.setenv(f"MINI_ORK_W_{k.upper()}", str(v))
    assert math.isclose(score(run_json), 0.60, abs_tol=1e-6)


def test_f07_explicit_weights_dict_overrides_env(monkeypatch):
    """Caller-supplied ``weights`` dict wins over env."""
    run_json = '{"success": true, "verifier_score": 1.0}'
    weights = {
        "success": 0.50,
        "verifier": 0.40,
        "quality": 0.05,
        "cost": 0.02,
        "latency": 0.02,
        "risk": 0.01,
    }
    monkeypatch.setenv("MINI_ORK_W_SUCCESS", "0.99")  # must be ignored
    # 0.50*1 + 0.40*1.0 + 0.05*0.5 = 0.925
    assert math.isclose(score(run_json, weights=weights), 0.925, abs_tol=1e-6)


def test_f08_empty_input_defaults():
    """Empty JSON — only quality_score default (0.5) contributes positively."""
    # 0.15 * 0.5 = 0.075
    assert math.isclose(score('{}'), 0.075, abs_tol=1e-6)


@pytest.mark.parametrize(
    "raw_success,expected",
    [
        ("true", 0.525),       # 0.45*1 + 0.15*0.5
        ("1", 0.525),
        ('"true"', 0.525),
        ('"1"', 0.525),
        ("false", 0.075),      # 0.45*0 + 0.15*0.5
        ("0", 0.075),
    ],
)
def test_f09_success_truthiness_matrix(raw_success: str, expected: float):
    """The success membership test accepts True/1/'true'/'1'; all four must
    yield 1.0 before weighting. Bool-False and 0 must yield 0.0."""
    run_json = '{"success": ' + raw_success + '}'
    assert math.isclose(score(run_json), expected, abs_tol=1e-6)


def test_score_clamped_to_unit_interval():
    """Huge penalties can't push U below 0; huge positives can't exceed 1."""
    assert score('{"success": false, "risk_penalty": 1.0, "cost_usd": 5,'
                 ' "max_cost_usd": 1, "duration_ms": 9, "max_duration_ms": 1,'
                 ' "quality_score": 0.0}') == 0.0


def test_smoke_import_and_score_no_io():
    """Pure-path smoke: importing the module and scoring a minimal fixture
    returns a float in [0, 1] without shelling out."""
    s = score('{"success": true}')
    assert isinstance(s, float)
    assert 0.0 <= s <= 1.0
