"""Pure-logic port of ``lib/utility_function.sh::utility_score``.

Faithful port of the **default formula branch** of the bash utility-score
function (Ch 18). The bash source is a thin shell that resolves a per-task
override and otherwise delegates to an embedded Python heredoc; this module
lifts that heredoc into a normal importable function and exposes the
deterministic helpers it depends on.

The per-task override path (``${MINI_ORK_HOME}/config/utility_functions/
<task_class>.sh``) is impure I/O and is intentionally **not** ported here.
Parity fixtures omit ``task_class`` so both bash and Python fall through to
the default formula; callers needing the override path should call the bash
function directly.

Public API::

    from mini_ork.ported.utility_function import score

    score(run_result_json, weights=None) -> float

``weights`` is a dict with any of ``success``, ``verifier``, ``quality``,
``cost``, ``latency``, ``risk``. Missing entries fall back to the
``MINI_ORK_W_*`` environment defaults (matching ``lib/utility_function.sh``'s
``_UTILITY_W_*`` constants).

Default formula (mirrors bash verbatim)::

    U = w_success*success + w_verifier*verifier_score + w_quality*quality_score
        - w_cost*norm_cost - w_latency*norm_latency - w_risk*risk_penalty

All components are clamped to ``[0.0, 1.0]`` before combination and ``U`` is
clamped to ``[0.0, 1.0]`` before return. Score rendering matches bash's
``f"{U:.6f}"`` format.

``tests/unit/test_utility_function_parity.py`` enforces float parity (within
``1e-6``) between this module and the live bash subprocess over a fixture
corpus of ``>=6`` cases.
"""

from __future__ import annotations

import json
import os
from typing import Mapping


_WEIGHT_KEYS = ("success", "verifier", "quality", "cost", "latency", "risk")

_DEFAULT_WEIGHTS = {
    "success": 0.45,
    "verifier": 0.20,
    "quality": 0.15,
    "cost": 0.10,
    "latency": 0.05,
    "risk": 0.05,
}

_ENV_WEIGHT_KEYS = {
    "success": "MINI_ORK_W_SUCCESS",
    "verifier": "MINI_ORK_W_VERIFIER",
    "quality": "MINI_ORK_W_QUALITY",
    "cost": "MINI_ORK_W_COST",
    "latency": "MINI_ORK_W_LATENCY",
    "risk": "MINI_ORK_W_RISK",
}


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _norm_ratio(value: float, ceiling: float) -> float:
    """Mirror bash: ``clamp(v / c)`` if c > 0, else 0.0."""
    if ceiling > 0:
        return _clamp(value / ceiling)
    return 0.0


def _resolve_weights(weights: Mapping[str, float] | None) -> dict[str, float]:
    """Caller ``weights`` dict wins, then ``MINI_ORK_W_*`` env, then hard defaults.

    Mirrors bash: ``_UTILITY_W_<NAME>="${MINI_ORK_W_<NAME>:-<default>}"``
    """
    resolved = dict(_DEFAULT_WEIGHTS)
    for key in _WEIGHT_KEYS:
        env_key = _ENV_WEIGHT_KEYS[key]
        env_val = os.environ.get(env_key)
        if env_val is not None:
            try:
                resolved[key] = float(env_val)
            except ValueError:
                pass
    if weights:
        for key in _WEIGHT_KEYS:
            if key in weights and weights[key] is not None:
                resolved[key] = float(weights[key])
    return resolved


def _parse_success(raw) -> float:
    """Match bash exactly: ``r.get("success") in (True, 1, "true", "1")``.

    JSON deserialization cannot produce Python ``True``, so this is
    effectively ``raw in (1, "true", "1")`` against the parsed JSON value,
    but we keep the literal membership test to mirror the source verbatim.
    """
    return _clamp(1.0 if raw in (True, 1, "true", "1") else 0.0)


def score(run_result_json: str, weights: Mapping[str, float] | None = None) -> float:
    """Compute the utility score for a completed run.

    Parameters
    ----------
    run_result_json:
        JSON string with fields consumed by ``lib/utility_function.sh``:
        ``success`` (bool | 0/1), ``verifier_score``, ``quality_score``,
        ``cost_usd``, ``max_cost_usd``, ``duration_ms``, ``max_duration_ms``,
        ``risk_penalty``, ``task_class``. ``task_class`` is ignored here —
        only the default-formula path is ported.
    weights:
        Optional override dict for the six weight constants. Missing entries
        fall through to ``MINI_ORK_W_*`` env vars, then hard defaults
        (0.45/0.20/0.15/0.10/0.05/0.05).

    Returns
    -------
    float
        Score in ``[0.0, 1.0]`` — exact parity with bash's
        ``f"{U:.6f}"`` rendering.
    """
    try:
        run_result = json.loads(run_result_json)
    except json.JSONDecodeError:
        raise ValueError(f"utility_score: invalid JSON: {run_result_json!r}")

    w = _resolve_weights(weights)

    success = _parse_success(run_result.get("success"))
    verifier_score = _clamp(run_result.get("verifier_score", 0.0))
    quality_score = _clamp(run_result.get("quality_score", 0.5))
    risk_penalty = _clamp(run_result.get("risk_penalty", 0.0))

    cost_usd = float(run_result.get("cost_usd", 0.0))
    max_cost = float(run_result.get("max_cost_usd", cost_usd if cost_usd > 0 else 1.0))
    norm_cost = _norm_ratio(cost_usd, max_cost)

    duration_ms = float(run_result.get("duration_ms", 0.0))
    max_duration = float(
        run_result.get("max_duration_ms", duration_ms if duration_ms > 0 else 1.0)
    )
    norm_latency = _norm_ratio(duration_ms, max_duration)

    u = (
        w["success"] * success
        + w["verifier"] * verifier_score
        + w["quality"] * quality_score
        - w["cost"] * norm_cost
        - w["latency"] * norm_latency
        - w["risk"] * risk_penalty
    )

    return _clamp(u)