"""UCCI — a calibrated error probability turns escalation into arithmetic.

Ported from 2605.18796. The router picks a lane, but nothing in that pipeline
states how likely the pick is to be *wrong*; escalation to the strong lane is
guesswork. UCCI fixes that by fitting an isotonic regression — a monotone map —
from the router's own margin to the observed error rate, so the margin reads as
an actual error probability. Escalate when the calibrated error crosses a
target; otherwise keep the cheap lane.

The margin is the gap between the winner's router score and the runner-up's,
persisted on ``execution_traces.route_margin`` (migration 0057). It is a proxy
for the logit margin the paper uses, because mini-ork's lanes return text over
different transports and most expose no token logprobs. The fit is therefore
only as good as that stand-in, and it decays when a lane's model or prompt
changes, so it is refit from recent rows rather than fitted once: a recency
window and a per-slice row cap keep a lane's pre-change history out of the map.
Both bounds can only be tightened from the environment.

No numpy: PAV is twenty lines of list arithmetic and the runtime is pure Python.

Everything here is a *measurement* or an *abstention* — a thin slice or a
missing column yields ``None`` (do not escalate), which is today's behaviour.
"""
from __future__ import annotations

import bisect
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

# The escalation threshold: escalate when the calibrated error probability
# exceeds this. It is the cost constraint's solution expressed as a target
# rather than re-derived by constrained minimization on every decision.
DEFAULT_TARGET_ERROR = 0.15
# Below this many observations a fit is noise, not calibration.
DEFAULT_MIN_SAMPLES = 12
# Refitting on every dispatch would open a connection per node for no benefit;
# the map moves only as rows accumulate.
DEFAULT_CACHE_TTL = 60.0
# A lane's rows from before its model or prompt changed describe a lane that no
# longer exists, so only rows inside this window may calibrate it.
DEFAULT_WINDOW_DAYS = 30.0
# The window bounds staleness by date; this bounds volume, so one busy lane
# cannot swamp a slice with rows the window would otherwise admit.
DEFAULT_MAX_ROWS = 500

_CACHE: dict = {}


def _enabled() -> bool:
    """UCCI is ON by default; ``MO_UCCI=0`` restores uncalibrated routing."""
    return os.environ.get("MO_UCCI", "1").strip().lower() not in ("0", "false", "no", "")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def target_error() -> float:
    """The escalation threshold, clamped so the env may only TIGHTEN it.

    A higher threshold tolerates more predicted error, i.e. escalates less. That
    is the dangerous direction, so it is not reachable from the environment: the
    env can only lower the bar and escalate sooner.
    """
    return min(DEFAULT_TARGET_ERROR,
               max(0.0, _env_float("MO_UCCI_TARGET_ERROR", DEFAULT_TARGET_ERROR)))


def min_samples() -> int:
    """Minimum observations for a fit, clamped so the env may only RAISE it.

    An env that could lower this to 1 would let a single observation define a
    monotone map — a fit that always agrees with itself and calibrates nothing.
    """
    return max(DEFAULT_MIN_SAMPLES, int(_env_float("MO_UCCI_MIN_SAMPLES",
                                                   DEFAULT_MIN_SAMPLES)))


def window_days() -> float:
    """Recency window in days, clamped so the env may only TIGHTEN it.

    A longer window re-admits rows written before a lane's model or prompt
    changed, which is the drift the window exists to exclude, so the
    environment cannot reach it. ``0`` admits no row at all and so abstains
    always; that is the behaviour ``MO_UCCI=0`` already gives and it is
    reachable only on purpose.
    """
    return min(DEFAULT_WINDOW_DAYS,
               max(0.0, _env_float("MO_UCCI_WINDOW_DAYS", DEFAULT_WINDOW_DAYS)))


def max_rows() -> int:
    """Per-slice row cap, clamped so the env may only LOWER it.

    More rows is not more evidence when the extra ones are stale: the cap is
    the same decay the window bounds by date, expressed as a bound on volume
    for a slice that is simply busy.
    """
    return min(DEFAULT_MAX_ROWS,
               max(1, int(_env_float("MO_UCCI_MAX_ROWS", DEFAULT_MAX_ROWS))))


def recent_cutoff() -> str:
    """Lower bound on ``created_at`` for the recency window.

    Formatted the way ``execution_traces.created_at`` is written
    (``%Y-%m-%dT%H:%M:%fZ``), so the comparison is a plain lexicographic one
    and needs no date parsing. A row in the cutoff second still compares
    greater, because the written value carries a fractional part.
    """
    return (datetime.now(timezone.utc) - timedelta(days=window_days())
            ).strftime("%Y-%m-%dT%H:%M:%S")


# ── isotonic regression (pool-adjacent-violators) ────────────────────────────

def pav(values: list[float]) -> list[float]:
    """Least-squares non-decreasing fit of ``values``, equal weights.

    PAV merges adjacent blocks whose means violate monotonicity into their
    weighted mean, left to right. The result is the closest non-decreasing
    sequence in L2 and — unlike a moving average or a spline — it cannot
    introduce a spurious non-monotonic wiggle, which is the whole point of using
    it for a calibration map.
    """
    blocks: list[list[float]] = []  # [weight, weighted_sum, count]
    for v in values:
        blocks.append([1.0, float(v), 1])
        while len(blocks) >= 2:
            prev_mean = blocks[-2][1] / blocks[-2][0]
            cur_mean = blocks[-1][1] / blocks[-1][0]
            if prev_mean <= cur_mean:
                break
            b = blocks.pop()
            a = blocks.pop()
            blocks.append([a[0] + b[0], a[1] + b[1], a[2] + b[2]])
    out: list[float] = []
    for weight, total, count in blocks:
        out.extend([total / weight] * int(count))
    return out


def fit_error_map(rows) -> tuple[list[float], list[float]]:
    """Fit ``margin -> error probability`` from ``(margin, is_error)`` pairs.

    Returns ``(margins, fitted)`` sorted by margin ascending with ``fitted``
    non-increasing: a larger margin is a more decisive win, so the fitted error
    must never rise as the margin grows. The monotonicity is not a nicety — an
    error probability that increased with confidence would make escalation
    decisions incoherent.
    """
    pts = sorted((float(m), 1.0 if e else 0.0) for m, e in rows)
    if not pts:
        return [], []
    margins = [p[0] for p in pts]
    errors = [p[1] for p in pts]
    # PAV fits non-decreasing; reversing turns that into non-increasing.
    fitted = list(reversed(pav(list(reversed(errors)))))
    return margins, fitted


def error_probability(margins: list[float], fitted: list[float],
                      margin: float) -> float | None:
    """Interpolate the calibrated error at ``margin``; ``None`` on an empty map.

    Linear interpolation between fitted buckets, clamped at both ends. The
    fitted sequence is non-increasing, so this is non-increasing too, which is
    what keeps the escalation rule a threshold rule.
    """
    if not margins or not fitted or len(margins) != len(fitted):
        return None
    if margin <= margins[0]:
        return fitted[0]
    if margin >= margins[-1]:
        return fitted[-1]
    i = bisect.bisect_left(margins, margin)
    x0, x1 = margins[i - 1], margins[i]
    y0, y1 = fitted[i - 1], fitted[i]
    if x1 == x0:
        return y1
    t = (margin - x0) / (x1 - x0)
    return y0 + t * (y1 - y0)


# ── data access ──────────────────────────────────────────────────────────────

def load_margin_rows(db: str, task_class: str, lane: str = "") -> list:
    """``(route_margin, is_error)`` for rows that carry a router margin.

    Only rows with a recorded margin can calibrate anything: a static route and
    an exploration swap both leave it NULL by design, and a row with no margin
    is not a low-confidence row, it is an uncalibratable one. ``is_error`` is
    ``status != 'success'`` — the outcome the router is trying to predict.

    ``lane`` filters on ``agent_version_id``, the lane the router dispatched.
    Node type is deliberately not a filter: it lives inside ``verifier_output``
    JSON rather than a column, and lane is the unit the map is a property of.

    Only rows inside the recency window are read, and at most ``max_rows()`` of
    them, newest first. The map describes the lane's *current* model and prompt,
    so rows from before a change pull the fit toward an error rate the lane no
    longer has. The paper calibrates on a static batch and names continual
    recalibration as the open piece; the window plus cap is the cheap stand-in
    for it, not a solution to it.

    An older database without the column returns ``[]`` (fail open), so a
    migration lag degrades routing to today's behaviour instead of raising.
    """
    if not db or not os.path.isfile(db):
        return []
    where = ["task_class = ?", "route_margin IS NOT NULL", "created_at >= ?"]
    args: list = [task_class, recent_cutoff()]
    if lane:
        where.append("agent_version_id = ?")
        args.append(lane)
    args.append(max_rows())
    sql = (f"SELECT route_margin, status FROM execution_traces "
           f"WHERE {' AND '.join(where)} "
           f"ORDER BY created_at DESC LIMIT ?")
    con = sqlite3.connect(db)
    con.execute("PRAGMA busy_timeout=5000")
    try:
        rows = con.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        # No route_margin column — this database predates migration 0057.
        con.close()
        return []
    con.close()
    return [(float(m), str(s or "") != "success") for m, s in rows if m is not None]


def _fit(db: str, task_class: str, lane: str):
    """Cached ``(margins, fitted)`` for one slice, or ``(None, None)``."""
    ttl = _env_float("MO_UCCI_CACHE_TTL", DEFAULT_CACHE_TTL)
    key = (db, task_class, lane)
    now = time.time()
    if ttl > 0:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1], hit[2]
    rows = load_margin_rows(db, task_class, lane)
    if len(rows) < min_samples():
        margins, fitted = None, None
    else:
        margins, fitted = fit_error_map(rows)
    if ttl > 0:
        _CACHE[key] = (now, margins, fitted)
    return margins, fitted


def clear_cache() -> None:
    """Drop cached fits (tests, and any caller that just wrote new rows)."""
    _CACHE.clear()


def calibrated_error(db: str, task_class: str, margin, lane: str = "") -> float | None:
    """Predicted error probability for ``lane`` winning by ``margin``.

    Per-lane first, because the map is a property of the model that produced the
    pick and different lanes have different error rates at the same margin.
    Falls back to the pooled slice when the lane's own history is too thin to
    fit. ``None`` — meaning "do not escalate" — when neither slice supports a
    fit, when UCCI is off, or when the pick recorded no margin.
    """
    if not _enabled() or margin is None:
        return None
    try:
        m = float(margin)
    except (TypeError, ValueError):
        return None
    if lane:
        margins, fitted = _fit(db, task_class, lane)
        if margins:
            return error_probability(margins, fitted, m)
    margins, fitted = _fit(db, task_class, "")
    if margins:
        return error_probability(margins, fitted, m)
    return None


def should_escalate(db: str, task_class: str, margin,
                    lane: str = "") -> tuple[bool, float | None]:
    """``(escalate, predicted_error)`` for a lane chosen with this margin.

    Escalates only on a positive prediction above the target. No fit, no
    margin, or UCCI disabled all return ``(False, None)`` — the uncalibrated
    behaviour the router had before this module existed.
    """
    p = calibrated_error(db, task_class, margin, lane=lane)
    if p is None:
        return False, None
    return p > target_error(), p
