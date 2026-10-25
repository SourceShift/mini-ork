"""Learn-Then-Test confidence bar for the self-driving escalation policy.

``_ltt_min_confidence`` computes the smallest confidence threshold whose
``(k+1)/(n+1)`` upper bound on the false-positive rate stays within a risk
level (Bates et al. 2021). ``mini_ork.recovery.escalation`` uses it, with
``alpha()``, to calibrate the confidence a model diagnosis needs before it may
open a framework-edit fix epic (``recovery/escalation_policy.py``).

History: this module used to also register an ``abstain_gate`` gate evaluator
that deferred low-confidence verifier passes to the oversight inbox. It was
removed in 2026-10 (docs/plans/2026-10-25-ecosystem-section6.md): the only way
to switch it on was a recipe's per-node ``gates:`` list, which the executor
never reads, and no verifier writes a confidence or a label, so it never had
data. The split-conformal sibling (``conformal_gate``) was removed with it.
The file keeps its name because it is listed in the frozen kernel.

Env knobs (one-way clamps, mirroring ``dispatch/calibration.py``):

  * ``MO_ABSTAIN_ALPHA``       — risk level, default 0.05, tighten-only.
  * ``MO_ABSTAIN_MIN_CALIB``   — minimum labelled rows, default 30, raise-only.
"""

from __future__ import annotations

import os
from typing import Optional

# Default risk level. Tighten-only: a higher alpha tolerates more
# calibrated risk, which is the dangerous direction (more false
# completions), so the env may only lower the value.
DEFAULT_ALPHA = 0.05
# Default minimum labelled rows for a calibrated threshold. Raise-only:
# a single observation would self-validate. Mirrors UCCI's
# ``calibration.min_samples()`` shape (calibration.py:84-91).
DEFAULT_MIN_CALIB = 30


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
