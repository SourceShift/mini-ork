"""Parity gate: ``mini_ork.ported.utility_function.score`` vs ``bash lib/utility_function.sh``.

For each fixture we invoke the LIVE bash function via subprocess (no mocking,
exactly as the production runtime would), capture stdout as a float, then call
the Python port with the same JSON string and assert
``|bash_score - python_score| < 1e-6``.

The bash ``utility_score`` checks for a per-task override under
``${MINI_ORK_HOME}/config/utility_functions/<task_class>.sh`` and dispatches
to it if present. That path is impure I/O and is intentionally not ported;
every fixture here omits ``task_class`` so both bash and Python fall through
to the default formula branch.

Strangler-fig co-existence is preserved: ``lib/utility_function.sh`` is
byte-identical before and after this test exists. The test only WRITES to
its ``tmp_path`` (not used here) and READS from ``lib/utility_function.sh``.
"""

from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path

import pytest

from mini_ork.ported.utility_function import score

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIB_UTILITY = REPO_ROOT / "lib" / "utility_function.sh"


def _run_bash_score(run_json: str, env_overrides: dict[str, str] | None = None) -> float:
    """Shell out to bash and invoke ``utility_score <json>`` against the live file.

    ``env_overrides`` is merged on top of the current process env so per-test
    weight overrides (``MINI_ORK_W_*``) reach bash exactly as a runtime
    caller would set them. No mocking — the bash function runs verbatim.
    """
    env = os.environ.copy()
    env.pop("MINI_ORK_W_SUCCESS", None)
    env.pop("MINI_ORK_W_VERIFIER", None)
    env.pop("MINI_ORK_W_QUALITY", None)
    env.pop("MINI_ORK_W_COST", None)
    env.pop("MINI_ORK_W_LATENCY", None)
    env.pop("MINI_ORK_W_RISK", None)
    if env_overrides:
        env.update(env_overrides)

    proc = subprocess.run(["bash", "-c", f". '{REPO_ROOT}/lib/utility_function.sh' && utility_score '{run_json}'"], cwd=str(REPO_ROOT), env=env, check=True, capture_output=True, text=True)
    return float(proc.stdout.strip())


def _parity(run_json: str, weights: dict | None = None,
            env_overrides: dict[str, str] | None = None) -> tuple[float, float]:
    """Run both sides with identical inputs; return (bash, python)."""
    bash_score = _run_bash_score(run_json, env_overrides=env_overrides)
    py_score = score(run_json, weights=weights)
    return bash_score, py_score


def _assert_close(bash_score: float, py_score: float, label: str) -> None:
    assert math.isclose(bash_score, py_score, abs_tol=1e-6), (
        f"parity drift [{label}]: bash={bash_score!r} py={py_score!r}"
    )


def test_f01_success_true_with_full_fields():
    """success=True + verifier + quality + cost + duration + risk — the meaty fixture."""
    run_json = (
        '{"success": true, "verifier_score": 0.8, "quality_score": 0.7,'
        ' "cost_usd": 0.5, "max_cost_usd": 1.0, "duration_ms": 5000,'
        ' "max_duration_ms": 10000, "risk_penalty": 0.1}'
    )
    b, p = _parity(run_json)
    _assert_close(b, p, "f01_success_true_full")


def test_f02_success_false_with_quality_default():
    """success=False; verifier_score/risk_penalty default to 0; quality_score defaults to 0.5."""
    run_json = '{"success": false}'
    b, p = _parity(run_json)
    _assert_close(b, p, "f02_success_false")
    # Sanity: known bash result for this fixture.
    assert math.isclose(b, 0.075, abs_tol=1e-6)


def test_f03_verifier_score_clamped_to_one():
    """verifier_score > 1 must clamp to 1.0 before weighting."""
    run_json = '{"success": true, "verifier_score": 1.7}'
    b, p = _parity(run_json)
    _assert_close(b, p, "f03_verifier_clamp")


def test_f04_custom_max_cost_usd_normalization():
    """Custom ceiling changes norm_cost denominator."""
    run_json = '{"success": true, "cost_usd": 0.25, "max_cost_usd": 0.5}'
    b, p = _parity(run_json)
    _assert_close(b, p, "f04_custom_max_cost")


def test_f05_custom_max_duration_ms_normalization():
    """Custom ceiling changes norm_latency denominator."""
    run_json = '{"success": true, "duration_ms": 3000, "max_duration_ms": 6000}'
    b, p = _parity(run_json)
    _assert_close(b, p, "f05_custom_max_duration")


def test_f06_weight_override_via_env():
    """Bash reads MINI_ORK_W_* from env at source time; the Python port
    reads them at call time. To make both sides agree we thread the same
    overrides through BOTH mechanisms: env to bash, dict to Python, then
    verify parity.

    Bash evaluates ``_UTILITY_W_<NAME>="${MINI_ORK_W_<NAME>:-<default>}"``
    once when the file is sourced, so subprocess must re-source with the
    overrides in env. The Python side's ``weights`` parameter is the
    equivalent for in-process callers.
    """
    run_json = '{"success": true, "verifier_score": 0.5, "quality_score": 0.5}'
    weights = {
        "success": 0.30,
        "verifier": 0.30,
        "quality": 0.30,
        "cost": 0.03,
        "latency": 0.03,
        "risk": 0.04,
    }
    env_overrides = {f"MINI_ORK_W_{k.upper()}": str(v) for k, v in weights.items()}
    b, p = _parity(run_json, weights=weights, env_overrides=env_overrides)
    _assert_close(b, p, "f06_env_weight_override")


def test_f07_explicit_weights_dict_overrides_env():
    """Caller-supplied ``weights`` dict wins over env on the Python side.

    Bash has no equivalent parameter (it reads env only); we re-mirror this
    fixture by passing the same weights through env on the bash side so
    both produce the same score.
    """
    run_json = '{"success": true, "verifier_score": 1.0}'
    weights = {
        "success": 0.50,
        "verifier": 0.40,
        "quality": 0.05,
        "cost": 0.02,
        "latency": 0.02,
        "risk": 0.01,
    }
    env_overrides = {f"MINI_ORK_W_{k.upper()}": str(v) for k, v in weights.items()}
    b, p = _parity(run_json, weights=weights, env_overrides=env_overrides)
    _assert_close(b, p, "f07_explicit_weights")


def test_f08_empty_input_defaults():
    """Empty JSON — only quality_score default (0.5) contributes positively."""
    run_json = '{}'
    b, p = _parity(run_json)
    _assert_close(b, p, "f08_empty_defaults")
    # 0.15 * 0.5 = 0.075 — confirmed against bash.
    assert math.isclose(b, 0.075, abs_tol=1e-6)


@pytest.mark.parametrize(
    "raw_success,expected_label",
    [
        ("true", "json_bool_true"),
        ("1", "json_int_one"),
        ('"true"', "json_string_true"),
        ('"1"', "json_string_one"),
        ("false", "json_bool_false"),
        ("0", "json_int_zero"),
    ],
)
def test_f09_success_truthiness_matrix(raw_success: str, expected_label: str):
    """Bash's success membership test accepts True/1/'true'/'1'; all four must
    yield 1.0 before weighting. Bool-False and 0 must yield 0.0."""
    run_json = '{"success": ' + raw_success + '}'
    b, p = _parity(run_json)
    _assert_close(b, p, f"f09_{expected_label}")


def test_smoke_import_and_score_no_io():
    """Pure-path smoke: importing the module and scoring a minimal fixture
    returns a float in [0, 1] without shelling out."""
    s = score('{"success": true}')
    assert isinstance(s, float)
    assert 0.0 <= s <= 1.0