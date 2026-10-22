"""RouteMoA pre-inference dominance screening.

RouteMoA (arXiv 2601.18130) screens candidate models with a cheap learned
predictor BEFORE inference and drops the ones that cannot win. mini-ork's
router ranks lanes from learned per-lane stats, but exploration keeps spending
real dispatches on lanes the data already rules out.

This module adds the missing screen. Given per-lane ``(success_count,
runs_count)`` from ``agent_performance_memory``, it puts a Wilson score
interval around each lane's observed success rate and drops a lane whose
interval sits strictly below another candidate's. The comparison is
conservative on purpose — intervals must separate, not just order — so a
screened lane is one the evidence already rules out, not one that merely lost
a point estimate.

Exploration is preserved: a lane with fewer than ``min_runs`` runs is never
screened because its interval is still too wide to rule it out. The screen can
therefore never empty the candidate list — with any finite set of intervals the
lane with the highest lower bound cannot be dominated.

Default ON; ``MO_ROUTEMOA=0`` opts out. ``MO_ROUTEMOA_MIN_RUNS`` may only
tighten the floor (values below the default 8 are clamped up), so opting into
the screen can never make it more aggressive than the reviewed default.
"""

from __future__ import annotations

import math
import os

DEFAULT_Z = 1.96
DEFAULT_MIN_RUNS = 8


def wilson_bounds(successes: int, n: int, z: float = DEFAULT_Z) -> tuple[float, float]:
    """Wilson score interval ``(lower, upper)`` for ``successes`` out of ``n``.

    ``n <= 0`` has no evidence to bound, so the whole unit interval is
    returned — the honest answer, and the one that keeps a lane with no runs
    from being screened.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = min(max(successes / n, 0.0), 1.0)
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    margin = (z / denom) * math.sqrt(
        p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return (max(0.0, center - margin), min(1.0, center + margin))


def _lane_of(candidate) -> str:
    """Read the lane name the same way ``_select_best_lane_scored`` does:
    position 0 of the candidate row (``agent_version_id``)."""
    return candidate[0]


def screen(candidates: list, stats: dict[str, tuple[int, int]], *,
           min_runs: int) -> tuple[list, list[dict]]:
    """Drop candidates statistically dominated by another candidate.

    ``candidates`` is the list ``_select_best_lane_scored`` consumes; elements
    are returned untouched and in their original order. ``stats`` maps lane ->
    ``(success_count, runs_count)``.

    A candidate is DOMINATED when both it and some other candidate have
    ``runs_count >= min_runs`` and its Wilson upper bound is strictly below
    that other candidate's Wilson lower bound. ``screened`` reports one entry
    per dropped lane: ``{"lane", "upper", "dominated_by", "dominator_lower"}``.

    The kept list is never empty while ``candidates`` is non-empty, and lanes
    below ``min_runs`` are never screened — they are still being explored.
    """
    bounds: dict[str, tuple[float, float]] = {}
    eligible: list[str] = []
    for cand in candidates:
        lane = _lane_of(cand)
        if lane in bounds:
            continue
        entry = stats.get(lane)
        if entry is None:
            continue
        try:
            successes, runs = int(entry[0]), int(entry[1])
        except (TypeError, ValueError, IndexError):
            continue
        if runs < min_runs:
            continue
        bounds[lane] = wilson_bounds(successes, runs)
        eligible.append(lane)

    kept: list = []
    screened: list[dict] = []
    for cand in candidates:
        lane = _lane_of(cand)
        own = bounds.get(lane)
        if own is None:
            kept.append(cand)
            continue
        dominator = None
        dominator_lower = 0.0
        for other in eligible:
            if other == lane:
                continue
            lower = bounds[other][0]
            if own[1] < lower and (dominator is None or lower > dominator_lower):
                dominator, dominator_lower = other, lower
        if dominator is None:
            kept.append(cand)
            continue
        screened.append({
            "lane": lane,
            "upper": own[1],
            "dominated_by": dominator,
            "dominator_lower": dominator_lower,
        })

    if candidates and not kept:
        # Unreachable with well-formed intervals (the highest lower bound
        # cannot be dominated), but the contract is absolute: never hand the
        # selector an empty list. Fall back to the first candidate unscreened.
        kept = [candidates[0]]
        first_lane = _lane_of(candidates[0])
        screened = [e for e in screened if e["lane"] != first_lane]
    return kept, screened


def enabled() -> bool:
    """RouteMoA dominance screening is ON by default; exact ``"0"`` opts out."""
    return os.environ.get("MO_ROUTEMOA") != "0"


def min_runs() -> int:
    """Minimum runs before a lane may be screened (default 8, tighten-only)."""
    try:
        value = int(os.environ.get("MO_ROUTEMOA_MIN_RUNS", str(DEFAULT_MIN_RUNS)))
    except (TypeError, ValueError):
        value = DEFAULT_MIN_RUNS
    return max(DEFAULT_MIN_RUNS, value)
