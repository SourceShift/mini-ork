"""Split-conformal accept/abstain gate over oracle non-conformity scores.

``abstain_gate`` already ships an LTT (Learn-Then-Test) abstention rule
over verifier *confidence*. This module is the complementary gate: a
split-conformal accept/abstain decision over the oracle's own
**non-conformity score** (``1 - confidence``, with wrong rows pinned at
``1.0``). A *lower* score means *more conforming* — i.e. more
trustworthy — so the gate accepts a pass when its score is at or below
the calibrated conformal threshold and defers when it exceeds it.

The threshold is the standard split-conformal quantile: the
``ceil((n + 1) * (1 - alpha))``-th smallest calibration score, clamped
into ``[0, n - 1]``. Delegating that index arithmetic to
``abstain_gate.conformal_threshold`` (a public alias added for this
gate) keeps one implementation of the math.

**Finite-sample claim.** With ``n >= min_calib`` exchangeable
calibration rows, split conformal gives a distribution-free
finite-sample coverage guarantee: a fresh score exceeds the threshold
with probability at most ``alpha``, so ``calibration_report`` may
report the lower bound ``1 - alpha``. Below ``min_calib`` rows no
calibration — and therefore no coverage claim — is made: the report
sets ``calibrated=False`` / ``coverage_lower=None``, and the evaluator
passes the verdict through unchanged.

Off by default. ``MO_CONFORMAL_ORACLE`` defaults to ``"0"``; when it is
off the evaluator returns the incoming verdict unchanged, so no
existing recipe changes behaviour. A recipe opts in by listing
``conformal_gate`` in its ``gates:`` array. The gate registers itself
through the OCP seam (``register_gate_evaluator``) at module load —
``gate_registry.py`` is not edited.

Env knobs (one-way clamps, mirroring ``abstain_gate`` so the gate cannot
be silently loosened from the environment):

  * ``MO_CONFORMAL_ORACLE``    — on/off switch, default off; accepts
    ``1/true/yes/on`` (case-insensitive).
  * ``MO_CONFORMAL_ALPHA``     — risk level, default 0.10, tighten-only
    (a value above 0.10 clamps to 0.10).
  * ``MO_CONFORMAL_MIN_CALIB`` — minimum calibration rows, default 30,
    raise-only (invalid values fall back to 30).

Evaluator contract: like ``abstain_gate._eval_abstain`` the registered
callable takes ``(condition, context_json, db_path, mini_ork_root)`` and
returns ``"pass" | "fail" | "defer"``. A ``fail`` verdict is returned
unchanged before any knob is read — this is load-bearing: calibration
may only ever demote a pass to defer, never lift a failure.
"""
from __future__ import annotations

import json
import math
import os
from typing import Optional

from mini_ork.gates import abstain_gate
from mini_ork.gates.gate_registry import register_gate_evaluator
from mini_ork.gates.oversight_inbox import enqueue

# Default risk level. Tighten-only: a higher alpha tolerates more
# calibrated risk (more false accepts), which is the dangerous
# direction, so the env may only lower it.
DEFAULT_ALPHA = 0.10
# Default minimum calibration rows for a calibrated threshold.
# Raise-only: a handful of rows cannot support a finite-sample
# coverage claim.
DEFAULT_MIN_CALIB = 30

# Truthy spellings accepted for MO_CONFORMAL_ORACLE.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def alpha() -> float:
    """Risk level, default :data:`DEFAULT_ALPHA`, clamped so the env may
    only TIGHTEN it (``min(DEFAULT_ALPHA, max(0, env))``)."""
    return min(
        DEFAULT_ALPHA,
        max(0.0, _env_float("MO_CONFORMAL_ALPHA", DEFAULT_ALPHA)),
    )


def min_calib() -> int:
    """Minimum calibration rows, default :data:`DEFAULT_MIN_CALIB`, clamped
    so the env may only RAISE it. Invalid values fall back to the default."""
    raw = os.environ.get("MO_CONFORMAL_MIN_CALIB")
    if raw is None:
        return DEFAULT_MIN_CALIB
    try:
        value = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_MIN_CALIB
    return max(DEFAULT_MIN_CALIB, value)


def oracle_enabled() -> bool:
    """Whether ``MO_CONFORMAL_ORACLE`` turns the gate on (default: off)."""
    raw = os.environ.get("MO_CONFORMAL_ORACLE")
    if raw is None:
        return False
    return raw.strip().lower() in _TRUE_VALUES


def nonconformity(
    *,
    confidence: float | None,
    wrong: bool = False,
) -> float:
    """Map a verdict row to a non-conformity score in ``[0, 1]``.

    A *lower* score means *more conforming* (more trustworthy) and is
    therefore the accept side of :func:`decide`.

    * ``wrong=True``            -> ``1.0`` (maximally non-conforming);
    * missing/``None``/invalid confidence -> ``1.0`` (most conservative);
    * otherwise                -> ``1.0 - clamp(confidence, 0, 1)``.

    ``NaN`` is treated as invalid rather than silently clamped.
    """
    if wrong:
        return 1.0
    if confidence is None:
        return 1.0
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return 1.0
    if math.isnan(value):
        return 1.0
    value = max(0.0, min(1.0, value))
    return float(1.0 - value)


def calibrate(scores: list[float], alpha: float) -> float:
    """Split-conformal threshold at the ``1 - alpha`` quantile.

    Delegates to :func:`mini_ork.gates.abstain_gate.conformal_threshold`
    so the index arithmetic lives in exactly one place. An empty list
    returns ``1.0`` (nothing can be accepted without calibration data).
    """
    return abstain_gate.conformal_threshold(scores, alpha)


def decide(score: float, threshold: float) -> str:
    """``"accept"`` iff ``score <= threshold`` (inclusive), else ``"abstain"``."""
    return "accept" if score <= threshold else "abstain"


def calibration_report(scores: list[float], alpha: float) -> dict:
    """Persisted-shape calibration summary with the finite-sample claim.

    Returns ``{"n", "alpha", "threshold", "coverage_lower",
    "min_calib", "calibrated"}``. ``coverage_lower`` is ``1 - alpha``
    only when ``n >= min_calib``; otherwise it is ``None`` and
    ``calibrated`` is ``False`` — no calibration data, no coverage
    claim.
    """
    n = len(scores)
    minimum = min_calib()
    calibrated = n >= minimum
    return {
        "n": n,
        "alpha": float(alpha),
        "threshold": calibrate(scores, alpha),
        "coverage_lower": (1.0 - alpha) if calibrated else None,
        "min_calib": minimum,
        "calibrated": calibrated,
    }


def _evaluate(
    condition: str,
    context_json: str,
    db_path: Optional[str],
    mini_ork_root: Optional[str],
) -> str:
    """Evaluator body — registered as ``conformal_gate`` at module load.

    Mirrors ``abstain_gate._eval_abstain``'s context/return contract and
    fail-safe defaults. Order is deliberate:

      1. Malformed context -> ``defer``.
      2. ``fail`` -> ``fail``, before any knob or calibration is read.
         Anything that is not a ``pass`` is ineligible and defers.
      3. ``MO_CONFORMAL_ORACLE`` off -> the verdict unchanged.
      4. Fewer than ``min_calib()`` labelled pass rows -> the verdict
         unchanged ("conformal_gate uncalibrated (n=<n>)"): no
         calibration -> no abstention claim.
      5. Otherwise calibrate, score this pass, and either accept it
         (return the original pass, threshold named in the module docs
         / audit evidence) or defer + enqueue the oversight item.
    """
    del mini_ork_root  # retained for caller-compatibility signature

    # 1. Parse context defensively — same fail-safe shape as abstain_gate.
    try:
        ctx = json.loads(context_json) if context_json else {}
        if not isinstance(ctx, dict):
            return "defer"
    except (TypeError, ValueError):
        return "defer"

    # 2. Read the verdict. NEVER lift ``fail``: calibration may only
    #    demote an eligible pass to defer. Only ``pass`` is eligible for
    #    calibration; ``defer`` and unknown verdicts are the safe default.
    raw_verdict = ctx.get("verifier_verdict", ctx.get("verdict", ""))
    verifier_verdict = str(raw_verdict) if raw_verdict is not None else ""
    if verifier_verdict == "fail":
        return "fail"
    if verifier_verdict != "pass":
        return "defer"

    # 3. Off-by-default knob: return the verdict unchanged so existing
    #    recipes behave byte-identically until they opt in.
    if not oracle_enabled():
        return verifier_verdict

    # 4. Load labelled pass history through the public abstain_gate
    #    delegate. Fewer than min_calib rows -> no calibration, no
    #    abstention claim, verdict unchanged.
    verifier_name = str(ctx.get("verifier_name", condition or "conformal_gate"))
    labelled = abstain_gate.load_labelled_passes(db_path, verifier_name)
    n = len(labelled)
    if n < min_calib():
        return verifier_verdict

    # 5. Build calibration scores from the labelled rows and this
    #    verdict's own confidence. Lower score = more conforming.
    scores = [
        nonconformity(confidence=confidence, wrong=wrong)
        for confidence, wrong in labelled
    ]
    risk = alpha()
    threshold = calibrate(scores, risk)
    score = nonconformity(confidence=ctx.get("confidence"), wrong=False)

    # 6. Accept -> original pass. Abstain -> defer + oversight row.
    if decide(score, threshold) == "accept":
        return verifier_verdict

    evidence = {
        "verifier_verdict": verifier_verdict,
        "verifier_name": verifier_name,
        "reason": "conformal-threshold-exceeded",
        "score": score,
        "threshold": threshold,
        "n": n,
        "alpha": risk,
        "task_class": ctx.get("task_class", ""),
        "run_id": ctx.get("run_id", ""),
        "lane": ctx.get("lane", ""),
    }
    try:
        enqueue(
            gate_id="conformal_gate",
            feature=verifier_name or condition or "conformal",
            phase="gate",
            context=evidence,
            blocks_dispatch_for=str(ctx.get("recipe", "") or ""),
            db_path=db_path,
        )
    except Exception:
        # An oversight-channel write failure must not turn the
        # calibrated defer into a fail; the defer is the verdict.
        pass
    return "defer"


def register() -> None:
    """Register the ``conformal_gate`` type via the OCP seam.

    ``gate_registry.py`` is not edited: a new gate type is registrable
    and evaluatable the moment an evaluator is registered.
    """
    register_gate_evaluator("conformal_gate", _evaluate)


# Module-level side effect, same pattern as ``abstain_gate``: importing
# this module registers the evaluator. Production boot opts in by
# importing the module (a follow-up wiring task); tests exercise the
# seam directly.
register()
