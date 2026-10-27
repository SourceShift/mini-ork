"""Collapse detector — a health signal other than the score being optimized.

arXiv 2606.21090 shows a self-improving loop can self-regress on the objective
it is optimizing: pass@1 rises then collapses toward zero while the loop's own
score keeps climbing, so watching that objective cannot see the collapse. The
loop needs a signal other than the score it is improving.

This module is the detector half of that signal: a pure, hermetic function over
a promotion history that reports whether the training-facing score and a frozen
anchor set have **diverged** (score rising while the anchor degrades), and
recommends a halt. The decision to act is left to the circuit breaker, which
this module deliberately does not touch — a detector that also acts cannot be
validated without acting.

No DB, no network, no lane, no model: the caller assembles the history; this
module reads it.
"""
from __future__ import annotations

from collections.abc import Sequence

MIN_STEPS = 4
MIN_RISE = 0.0
MIN_DROP = 0.0

# Mean-of-halves arithmetic accumulates float noise (0.2 becomes
# 0.1999999999999999); pin the reported values to 6 decimals so the paper's
# exact signature (score_rise 0.2, anchor_drop 0.225) survives == comparison.
_ROUND = 6


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _round(value: float) -> float:
    return round(value, _ROUND)


def _directive_counts(rows: Sequence[dict]) -> list[int]:
    """Per-step number of distinct directive ids."""
    return [len(set(row.get("directives") or [])) for row in rows]


def detect(history: Sequence[dict]) -> dict:
    """Report whether a promotion history shows the collapse signature.

    Each input row is one promotion step::

        {"step": int, "score": float, "anchor": float, "directives": Sequence[str]}

    ``score`` is the training-facing promotion score; ``anchor`` is the pass
    rate on the frozen anchor set at that step. Returns the report shape below.
    """
    n = len(history)
    rows = [
        {
            "step": row.get("step", i),
            "score": float(row["score"]),
            "anchor": float(row["anchor"]),
            "n_directives": len(set(row.get("directives") or [])),
        }
        for i, row in enumerate(history)
    ]

    all_directives = [d for row in history for d in (row.get("directives") or [])]
    directive_diversity = (
        len(set(all_directives)) / len(all_directives) if all_directives else None
    )

    if n < MIN_STEPS:
        return {
            "n": n,
            "min_steps": MIN_STEPS,
            "score_rise": None,
            "anchor_drop": None,
            "divergence": False,
            "collapse": False,
            "recommendation": "none",
            "directive_diversity": directive_diversity,
            "diversity_trend": None,
            "reason": "insufficient history (n < 4)",
            "rows": rows,
        }

    # Half-split means, not a fitted slope: with small n a least-squares slope
    # is dominated by the endpoints. Odd n puts the extra row in the second half.
    first = rows[: n // 2]
    second = rows[n // 2:]
    score_rise = _round(_mean([r["score"] for r in second])
                        - _mean([r["score"] for r in first]))
    anchor_drop = _round(_mean([r["anchor"] for r in first])
                         - _mean([r["anchor"] for r in second]))

    diversity_trend = _round(
        _mean(_directive_counts(history[n // 2:]))
        - _mean(_directive_counts(history[: n // 2]))
    )

    divergence = score_rise > MIN_RISE and anchor_drop > MIN_DROP
    collapse = divergence

    if collapse:
        recommendation = "halt"
        reason = "score rises while the anchor set degrades (collapse signature)"
        if diversity_trend < 0:
            reason += "; directive diversity narrowing"
    elif anchor_drop > 0:
        recommendation = "watch"
        reason = "anchor set degrades but the score is not rising"
    else:
        recommendation = "none"
        reason = "no divergence"

    return {
        "n": n,
        "min_steps": MIN_STEPS,
        "score_rise": score_rise,
        "anchor_drop": anchor_drop,
        "divergence": divergence,
        "collapse": collapse,
        "recommendation": recommendation,
        "directive_diversity": directive_diversity,
        "diversity_trend": diversity_trend,
        "reason": reason,
        "rows": rows,
    }


def summarize(report: dict) -> str:
    """Render a report as a short human-readable block. ``None`` renders ``-``."""
    def _fmt(value):
        return "-" if value is None else value

    return (
        f"n={report['n']} (min_steps={report['min_steps']})\n"
        f"score_rise={_fmt(report['score_rise'])} anchor_drop={_fmt(report['anchor_drop'])}\n"
        f"directive_diversity={_fmt(report['directive_diversity'])} "
        f"diversity_trend={_fmt(report['diversity_trend'])}\n"
        f"recommendation={report['recommendation']}\n"
        f"reason={report['reason']}"
    )


# --- Diversity / pass@k floor (RSI Tier-1 #12a) -----------------------------
#
# The detector above needs a score-vs-anchor history that nothing currently
# writes. The floor instead consumes terminal run outcomes that already exist:
# a task class whose pass@k falls while its implementer-lane set narrows is a
# diversity collapse. Both conditions are required — many recipes pin one lane
# by design, so narrowing alone carries no signal.
FLOOR_WINDOW = 20
FLOOR_K = 3
FLOOR_DROP = 0.2
FLOOR_MAX_LANES = 1


def _floor_half(rows: Sequence[dict], k: int) -> dict:
    """Summarize one half: pass@1, pass@k by task, and lane diversity."""
    n = len(rows)
    pass_at_1 = _round(sum(1 for r in rows if bool(r.get("passed"))) / n)

    # Group runs of the same task (first-appearance order): a task counts as
    # solved if any of its first k runs passed.
    groups: dict[str, list[bool]] = {}
    for i, row in enumerate(rows):
        key = str(row.get("task_key") or "")
        if not key:
            key = str(row.get("run_id") or "")
        if not key:
            key = f"#{i}"
        groups.setdefault(key, []).append(bool(row.get("passed")))
    pass_at_k = _round(
        sum(1 for group in groups.values() if any(group[:k])) / len(groups)
    )

    lanes = sorted(
        {
            str(x).strip()
            for row in rows
            for x in (row.get("lanes") or [])
            if str(x).strip()
        }
    )
    return {
        "n": n,
        "pass_at_1": pass_at_1,
        "pass_at_k": pass_at_k,
        "lane_count": len(lanes),
        "lanes": lanes,
    }


def floor(
    runs: Sequence[dict],
    *,
    window: int = FLOOR_WINDOW,
    k: int = FLOOR_K,
    drop: float = FLOOR_DROP,
    max_lanes: int = FLOOR_MAX_LANES,
) -> dict:
    """Report whether a task class is collapsing on pass@k and lane diversity.

    ``runs`` is oldest-first; each row is
    ``{"run_id", "passed", "task_key", "lanes"}``. Compares the most recent
    ``window`` terminal runs against the ``window`` before them. A ``halt``
    requires pass@k to fall by more than ``drop`` **and** the lane set to
    narrow to at most ``max_lanes``.
    """
    window = max(1, int(window))
    k = max(1, int(k))
    drop = float(drop)
    max_lanes = int(max_lanes)
    n = len(runs)

    report = {
        "n": n,
        "window": window,
        "k": k,
        "drop": drop,
        "max_lanes": max_lanes,
        "prior": None,
        "recent": None,
        "pass_at_k_drop": None,
        "fell": False,
        "narrowed": False,
        "breach": False,
        "recommendation": "none",
        "reason": f"insufficient history (n < {2 * window})",
    }

    if n < 2 * window:
        return report

    prior = _floor_half(runs[n - 2 * window:n - window], k)
    recent = _floor_half(runs[n - window:], k)
    pass_at_k_drop = _round(prior["pass_at_k"] - recent["pass_at_k"])
    fell = pass_at_k_drop > drop
    narrowed = (
        recent["lane_count"] < prior["lane_count"]
        and recent["lane_count"] <= max_lanes
    )
    breach = fell and narrowed

    if breach:
        recommendation = "halt"
        reason = "pass@k fell while lane diversity narrowed (diversity floor breached)"
    elif fell:
        recommendation = "watch"
        reason = "pass@k fell but lane diversity did not narrow"
    elif narrowed:
        recommendation = "watch"
        reason = "lane diversity narrowed but pass@k did not fall"
    else:
        recommendation = "none"
        reason = "no floor breach"

    report.update({
        "prior": prior,
        "recent": recent,
        "pass_at_k_drop": pass_at_k_drop,
        "fell": fell,
        "narrowed": narrowed,
        "breach": breach,
        "recommendation": recommendation,
        "reason": reason,
    })
    return report
