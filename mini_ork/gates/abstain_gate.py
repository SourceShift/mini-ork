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
  3. Computes a Learn-Then-Test (LTT) risk-controlling confidence
     threshold (Bates et al. 2021) over labelled pass rows: the
     smallest confidence bar ``t`` such that, among rows with
     confidence ≥ ``t``, the ``(k+1)/(n+1)`` upper bound on the
     empirical false-positive rate is ≤ alpha. A pass with confidence
     ≥ ``t`` passes; below it defers + enqueues. If no ``t`` satisfies
     the bound, the verifier is uncertifiable at alpha → every pass
     defers with reason ``verifier-uncertifiable-at-alpha``.
  4. With fewer than ``MO_ABSTAIN_MIN_CALIB`` labelled pass rows the
     verdict passes through unchanged: no calibration, no abstention
     claim (kickoff rule #3 — "no calibration → no abstention claims").
  5. A ``fail`` from the verifier is **never** lifted to ``pass``
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

    NB: retained for back-compat (tests ``test_5`` / ``test_5b``
    import this helper directly to exercise the split-conformal
    quantile on a synthetic uniform). The live ``_eval_abstain``
    body now uses the LTT risk-controlling threshold
    (``_ltt_min_confidence``) instead — split-conformal saturates at
    ``1.0`` whenever any labelled row scores ``1.0`` (a wrong row),
    which is precisely when the verifier is least trustworthy.
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


def _ltt_min_confidence(
    labelled: list[tuple[float, bool]],
    risk: float,
) -> tuple[float, bool, int, int, float]:
    """Learn-Then-Test (LTT) risk-controlling confidence threshold.

    Given labelled pass rows as ``(confidence, is_wrong)`` pairs, find
    the smallest confidence threshold ``t`` such that, among rows
    with confidence ≥ ``t``, the ``(k+1)/(n+1)`` upper bound on the
    empirical false-positive rate is ≤ ``risk``.

    This is the Bates-et-al.-2021 ``(k+1)/(n+1)`` rule (a one-sided
    Clopper-Pearson-style bound valid without ``scipy`` — the project
    standard for exact binomial statistics; see
    ``mini_ork/learning/hack_probe.py:24-77``).

    Returns ``(min_confidence, uncertifiable, accepted_n,
    accepted_wrong, bound)``:

      * ``min_confidence`` — smallest ``t`` satisfying the bound
        (0.0 if uncertifiable or empty).
      * ``uncertifiable`` — ``True`` iff NO candidate threshold has
        bound ≤ ``risk``; the caller then defers every pass.
      * ``accepted_n`` / ``accepted_wrong`` — counts at the chosen
        ``t`` (or at the full set if uncertifiable, for evidence).
      * ``bound`` — ``(k+1)/(n+1)`` at the chosen ``t`` (or at the
        full set if uncertifiable).

    An empty ``labelled`` list returns the permissive fallback
    ``(0.0, False, 0, 0, 0.0)``; the caller short-circuits on
    ``len(labelled) < min_required`` before this matters.
    """
    if not labelled:
        return (0.0, False, 0, 0, 0.0)

    n_total = len(labelled)
    k_total = sum(1 for _, w in labelled if w)

    # Unique confidence values in the labelled set, sorted descending
    # (most-restrictive threshold first). For each candidate ``t``
    # compute (k+1)/(n+1) over rows with confidence ≥ ``t``.
    unique_t = sorted({c for c, _ in labelled}, reverse=True)

    best_t: Optional[float] = None
    best_n = 0
    best_k = 0
    best_bound = 1.0
    for t in unique_t:
        n_sub = sum(1 for c, _ in labelled if c >= t)
        k_sub = sum(1 for c, w in labelled if c >= t and w)
        bound = (k_sub + 1) / (n_sub + 1)
        if bound <= risk:
            # Smallest ``t`` wins — keep iterating to find a smaller
            # candidate that still satisfies the bound.
            if best_t is None or t < best_t:
                best_t = t
                best_n = n_sub
                best_k = k_sub
                best_bound = bound

    if best_t is None:
        # No candidate threshold satisfies the bound → the verifier
        # is too unreliable at every confidence bar. The caller must
        # defer every pass and surface the full-set bound as evidence.
        total_bound = (k_total + 1) / (n_total + 1)
        return (0.0, True, n_total, k_total, total_bound)

    return (best_t, False, best_n, best_k, best_bound)


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


def _load_labelled_passes(
    db_path: Optional[str], verifier_name: str
) -> list[tuple[float, bool]]:
    """Labelled pass rows as ``(confidence, is_wrong)`` pairs.

    The LTT helper needs per-row confidence and a wrong/right flag —
    not the aggregated non-conformity score that ``_load_scores``
    returns. We restrict to ``verdict='pass'`` because the gate's
    decision is whether to trust a *pass*: a correct ``fail`` is a
    true negative, not part of the false-positive rate.

    Reads up to ``max_rows()`` rows, newest first. Missing or
    unparseable confidence defaults to ``0.0`` (most conservative —
    the LTT algorithm still bounds the FPR over them, treating them
    as low-confidence). Older DBs without ``verifier_results`` return
    ``[]`` (fail open) so the gate falls through to the pass-through
    branch rather than crashing.
    """
    if not db_path or not os.path.isfile(db_path):
        return []
    cap = max_rows()
    try:
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA busy_timeout=5000")
        try:
            rows = con.execute(
                "SELECT confidence, is_false_positive "
                "FROM verifier_results "
                "WHERE verifier_name = ? AND verdict = 'pass' "
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

    labelled: list[tuple[float, bool]] = []
    for confidence, is_fp in rows:
        is_wrong = bool(is_fp)
        if confidence is None:
            c = 0.0
        else:
            try:
                c = float(confidence)
            except (TypeError, ValueError):
                c = 0.0
        # Clamp into [0, 1]; the schema is TEXT in some installs.
        c = max(0.0, min(1.0, c))
        labelled.append((c, is_wrong))
    return labelled


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

    # 7. Compute LTT risk-controlling threshold and decide.
    # We need labelled pass rows (confidence + wrong flag), not the
    # aggregated non-conformity scores — those lose the per-row
    # confidence the (k+1)/(n+1) bound operates over.
    labelled = _load_labelled_passes(db_path, verifier_name)
    min_confidence, uncertifiable, accepted_n, accepted_wrong, bound = (
        _ltt_min_confidence(labelled, risk)
    )

    # 7a. Uncertifiable branch: no confidence bar satisfies the bound
    # → the verifier is too unreliable at every confidence. Every
    # pass defers; the evidence carries the full-set bound and the
    # ``reason`` marker downstream tooling keys off.
    if uncertifiable:
        evidence = {
            "verifier_verdict": verifier_verdict,
            "verifier_name": verifier_name,
            "confidence": confidence,
            "min_confidence": min_confidence,
            "threshold": bound,  # back-compat: was conformal threshold
            "alpha": risk,
            "n_calib": len(labelled),
            "accepted_n": accepted_n,
            "accepted_wrong": accepted_wrong,
            "bound": bound,
            "alpha_bound_met": False,
            "reason": "verifier-uncertifiable-at-alpha",
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
            pass
        return "defer"

    # 7b. Normal LTT branch: accept iff confidence ≥ min_confidence.
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
        "threshold": bound,  # back-compat: was conformal threshold
        "alpha": risk,
        "n_calib": len(labelled),
        "accepted_n": accepted_n,
        "accepted_wrong": accepted_wrong,
        "bound": bound,
        "alpha_bound_met": True,
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
