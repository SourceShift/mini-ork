"""Calibrated abstention gate.

A verifier that issues a "pass" it is not sure about should NOT ship a
confident verdict; it should defer to the human-oversight channel and
escalate. Mini-ork has the channel (``mini_ork.gates.oversight_inbox``)
and the verdict vocabulary (``pass | fail | defer``) but nothing wired
between them that reasons from the verifier's confidence.

This gate is the wiring. It registers itself as ``abstain_gate`` via
the OCP seam (``gate_registry.register_gate_evaluator``) — no edit to
``gate_registry.py`` is required. When asked to evaluate, it:

  1. Reads the verifier verdict and confidence from the context.
  2. Loads the verifier's labelled history from ``verifier_results``
     (the table added by migration 0025; verdict + confidence +
     ``is_false_positive`` / ``is_false_negative`` columns).
  3. Computes a split-conformal non-conformity score per row
     (``1 - confidence`` for a correct pass, ``1.0`` for a wrong one —
     a wrong prediction is maximally non-conforming regardless of how
     confident the verifier was), then takes the ``(1 - alpha)``
     quantile as the threshold.
  4. With fewer than ``MO_ABSTAIN_MIN_CALIB`` rows the verdict passes
     through unchanged: no calibration, no abstention claim
     (kickoff rule #3 — "no calibration → no abstention claims").
  5. With enough history, a ``pass`` whose confidence falls below the
     calibrated bar returns ``defer`` AND enqueues an oversight item.
     A ``fail`` from the verifier is **never** lifted to ``pass``
     (kickoff rule #4 — load-bearing invariant).

Env knobs (one-way clamps, mirroring ``dispatch/calibration.py`` so
the gate cannot be silently loosened from the environment):

  * ``MO_ABSTAIN_ALPHA``       — risk level, default 0.05, tighten-only.
  * ``MO_ABSTAIN_MIN_CALIB``   — minimum labelled rows, default 30, raise-only.

The gate is opt-in per recipe (kickoff rule #5): listing
``abstain_gate`` in a recipe's ``gates:`` array turns it on for that
recipe. No recipe is changed in this task.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from typing import Optional

from mini_ork.gates.gate_registry import register_gate_evaluator
from mini_ork.gates.oversight_inbox import enqueue

# Default risk level. Tighten-only: a higher alpha tolerates more
# calibrated risk, which is the dangerous direction (more false
# completions), so the env may only lower the value.
DEFAULT_ALPHA = 0.05
# Default minimum labelled rows for a calibrated threshold. Raise-only:
# a single observation would self-validate. Mirrors UCCI's
# ``calibration.min_samples()`` shape (calibration.py:84-91).
DEFAULT_MIN_CALIB = 30
# Per-slice row cap. Bounds volume the way the recency window bounds
# staleness — one busy verifier cannot swamp the calibration with stale
# rows the window would otherwise admit. Lower-only.
DEFAULT_MAX_ROWS = 500


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def alpha() -> float:
    """Risk level, clamped so the env may only TIGHTEN it.

    Same shape as ``calibration.target_error()``: ``min(DEFAULT,
    max(0, env))``. A higher alpha tolerates more calibrated risk,
    which is the dangerous direction, so the env can only lower it.
    """
    return min(DEFAULT_ALPHA, max(0.0, _env_float("MO_ABSTAIN_ALPHA", DEFAULT_ALPHA)))


def min_calib() -> int:
    """Minimum labelled rows for a calibration, clamped so the env may
    only RAISE it.

    Same shape as ``calibration.min_samples()``: ``max(DEFAULT,
    int(env))``. A single observation would self-validate and
    calibrate nothing.
    """
    return max(DEFAULT_MIN_CALIB,
               int(_env_float("MO_ABSTAIN_MIN_CALIB", DEFAULT_MIN_CALIB)))


def max_rows() -> int:
    """Per-slice row cap, clamped so the env may only LOWER it."""
    return min(DEFAULT_MAX_ROWS,
               max(1, int(_env_float("MO_ABSTAIN_MAX_ROWS", DEFAULT_MAX_ROWS))))


def _conformal_threshold(scores: list[float], risk: float) -> float:
    """Split-conformal threshold at the ``1 - risk`` quantile.

    Index follows the standard ``ceil((n+1) * (1 - alpha))``-th smallest,
    clamped into ``[0, n-1]``. With ``n`` non-conformity scores and a
    new exchangeable score, the guarantee is that the new score
    exceeds the threshold with probability at most ``alpha`` — the
    form most useful for "abstain when score exceeds threshold" rules.

    An empty score list returns ``1.0`` (no non-conformity observed
    means the most permissive threshold; the caller short-circuits on
    ``len(scores) < min_calib`` before this matters).
    """
    if not scores:
        return 1.0
    n = len(scores)
    sorted_scores = sorted(scores)
    # Standard split-conformal quantile index. Convert to 0-based.
    q = math.ceil((n + 1) * (1.0 - risk)) - 1
    if q < 0:
        q = 0
    if q >= n:
        q = n - 1
    return float(sorted_scores[q])


def _load_scores(db_path: Optional[str], verifier_name: str) -> list[float]:
    """Non-conformity scores for past ``verifier_name`` pass verdicts.

    Score per row:

      * ``1.0`` if the row was actually wrong (``is_false_positive=1``
        for a ``pass`` verdict, or ``is_false_negative=1`` for a
        ``fail`` verdict) — a wrong prediction is maximally
        non-conforming regardless of how confident the verifier was.
      * ``1 - confidence`` if the row carried a confidence value and
        was not flagged as wrong — low confidence is the canonical
        non-conformity signal for split-conformal calibration.
      * ``0.0`` otherwise (correct pass with no recorded confidence).

    Reads up to ``max_rows()`` rows, newest first. Older DBs without
    ``verifier_results`` or the ground-truth columns return ``[]``
    (fail open) so the gate falls through to the pass-through branch
    rather than crashing.
    """
    if not db_path or not os.path.isfile(db_path):
        return []
    cap = max_rows()
    try:
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA busy_timeout=5000")
        try:
            rows = con.execute(
                "SELECT verdict, confidence, is_false_positive, "
                "       is_false_negative "
                "FROM verifier_results "
                "WHERE verifier_name = ? AND verdict IN ('pass', 'fail') "
                "ORDER BY created_at DESC LIMIT ?",
                (verifier_name, cap),
            ).fetchall()
        except sqlite3.OperationalError:
            # Missing table or columns — old DB. Fail open.
            con.close()
            return []
        con.close()
    except Exception:
        return []

    scores: list[float] = []
    for verdict, confidence, is_fp, is_fn in rows:
        is_wrong = bool(is_fp) or bool(is_fn)
        if is_wrong:
            scores.append(1.0)
            continue
        if verdict != "pass":
            # A correct fail is not non-conforming — abstain calibration
            # is calibrated against false-positive passes, not against
            # accurate catches.
            scores.append(0.0)
            continue
        if confidence is None:
            scores.append(0.0)
            continue
        try:
            c = float(confidence)
        except (TypeError, ValueError):
            scores.append(0.0)
            continue
        # Clamp into [0, 1]; negative confidences or >1 are nonsensical
        # but the schema is TEXT in some installs, so guard at the read.
        c = max(0.0, min(1.0, c))
        scores.append(1.0 - c)
    return scores


def _calibrated_error_from_margin(
    db_path: Optional[str],
    ctx: dict,
) -> Optional[float]:
    """Fall back to UCCI's calibrated error when the context lacks an
    explicit confidence value.

    Returns a confidence (NOT an error probability) in [0, 1], or
    ``None`` when UCCI has nothing to say (the dispatcher treats
    ``None`` as "do not act" — same fail-open as
    ``calibration.calibrated_error`` itself).
    """
    try:
        from mini_ork.dispatch import calibration as _cal
    except Exception:
        return None
    # ``calibrated_error`` requires a concrete ``db`` path; defer when
    # the gate was invoked without one (the fall-through branch then
    # returns ``defer`` to the caller, which is the safe default).
    if not db_path:
        return None
    task_class = str(ctx.get("task_class", "") or "")
    lane = str(ctx.get("lane", "") or "")
    margin = ctx.get("route_margin")
    if not task_class or margin is None:
        return None
    try:
        err = _cal.calibrated_error(db_path, task_class, margin, lane=lane)
    except Exception:
        return None
    if err is None:
        return None
    try:
        return max(0.0, min(1.0, 1.0 - float(err)))
    except (TypeError, ValueError):
        return None


def _eval_abstain(
    condition: str,
    context_json: str,
    db_path: Optional[str],
    mini_ork_root: Optional[str],
) -> str:
    """Evaluator body — registered as ``abstain_gate`` at module load.

    Returns one of ``"pass" | "fail" | "defer"`` per the
    ``GateEvaluator`` signature (``gate_registry.py:288``). Defers on
    any malformed input — same fail-safe shape as
    ``_evaluate_liveness`` (``gate_registry.py:213-218``).
    """
    del mini_ork_root  # retained for caller-compatibility signature
    # 1. Parse context defensively.
    try:
        ctx = json.loads(context_json) if context_json else {}
        if not isinstance(ctx, dict):
            return "defer"
    except (TypeError, ValueError):
        return "defer"

    # 2. Read the verifier verdict. NEVER lift ``fail`` to ``pass`` —
    # this is the load-bearing invariant (kickoff rule #4). Early
    # return before any calibration touches it.
    raw_verdict = ctx.get("verifier_verdict", ctx.get("verdict", ""))
    verifier_verdict = str(raw_verdict) if raw_verdict is not None else ""
    if verifier_verdict == "fail":
        return "fail"
    if verifier_verdict not in ("pass", "defer"):
        # Empty / unknown verdict — we have nothing to abstain on.
        # Defer is the safe default (the caller can re-evaluate when
        # the verdict is well-formed).
        return "defer"

    # 3. Env knobs (one-way clamps — see module docstring).
    risk = alpha()
    min_required = min_calib()

    # 4. Load labelled history. The verifier_name is reused below
    # (defer-evidence payload); resolve it once.
    verifier_name = str(
        ctx.get("verifier_name", condition or "abstain_gate")
    )
    scores = _load_scores(db_path, verifier_name)

    # 5. Pass through when there is no calibration. Kickoff rule #3:
    # "with fewer than MO_ABSTAIN_MIN_CALIB labelled rows, the gate
    # passes through the verifier's verdict unchanged (no calibration
    # → no abstention claims)". This branch runs BEFORE any confidence
    # read: a missing confidence value is fine here — we have nothing
    # to abstain on, so we cannot claim an abstention.
    if len(scores) < min_required:
        return verifier_verdict

    # 6. Read confidence. Prefer the per-call value; fall back to the
    # calibrated-error map when the dispatcher recorded a route margin.
    # Calibration must exist by this point (step 5) — only a missing or
    # out-of-range confidence is a defer signal now.
    confidence = ctx.get("confidence")
    if confidence is None:
        confidence = _calibrated_error_from_margin(db_path, ctx)
    if confidence is None:
        return "defer"
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        return "defer"
    # Out-of-range confidence — treat as missing rather than guess.
    if confidence < 0.0 or confidence > 1.0 or math.isnan(confidence):
        return "defer"

    # 7. Compute threshold and decide.
    threshold = _conformal_threshold(scores, risk)
    # ``threshold`` is in non-conformity units (0 = most confident,
    # 1 = least confident). The matching confidence bar is therefore
    # ``1 - threshold``: a verifier with confidence BELOW this bar
    # falls into the alpha-tail and should defer.
    min_confidence = max(0.0, min(1.0, 1.0 - threshold))
    if confidence >= min_confidence:
        return verifier_verdict  # high-confidence pass → pass

    # 8. Defer + enqueue. The enqueue is best-effort: a write failure
    # does not convert the gate's defer into a fail. (The kickoff's
    # rule #4 forbids fail→pass; symmetric reasoning forbids enqueue
    # failure → fail when the calibrated verdict is "I am not sure".)
    evidence = {
        "verifier_verdict": verifier_verdict,
        "verifier_name": verifier_name,
        "confidence": confidence,
        "min_confidence": min_confidence,
        "threshold": threshold,
        "alpha": risk,
        "n_calib": len(scores),
        "task_class": ctx.get("task_class", ""),
        "run_id": ctx.get("run_id", ""),
        "lane": ctx.get("lane", ""),
    }
    try:
        enqueue(
            gate_id="abstain_gate",
            feature=verifier_name or condition or "abstain",
            phase="gate",
            context=evidence,
            blocks_dispatch_for=str(ctx.get("recipe", "") or ""),
            db_path=db_path,
        )
    except Exception:
        # Oversight-channel failure must not turn a defer into a fail.
        # The defer is the calibrated verdict; the inbox row is the
        # operator-facing audit, and missing it is a logged problem,
        # not a gate re-decision.
        pass
    return "defer"


# Module-level side effect: register the OCP evaluator. ``gate_registry``
# must be imported first (it provides ``register_gate_evaluator``) —
# gate_bootstrap.py imports this module AFTER importing ``gate_registry``
# transitively via ``native_gates``, so the dependency is satisfied by
# the time this line runs.
register_gate_evaluator("abstain_gate", _eval_abstain)
