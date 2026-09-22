"""Anchor-discipline audit for an evolved eval metric (arXiv 2607.12790).

Every self-improving loop selects on an eval metric it treats as ground truth,
but the metric is itself a trained artifact — a grader nobody graded. "Who
Grades the Grader?" finds the guard that keeps such a metric honest is not its
training but its *anchor discipline*: the metric must carry a held-out anchor
set it never reads, and it must be audited against that anchor by an outer
party that also never reads it. Removing the anchor guards collapses the metric
into a vacuous detector (one that agrees with everything); removing the
lifecycle does not.

This module builds the *audit* half of that claim. Given a caller-assembled
history of metric generations, it reports whether the metric still has an
anchor it never read and whether its discrimination has collapsed toward
vacuity. It measures; it does not evolve, retrain, replace, or promote the
metric. The metric loop that *searches* detector compositions is a separate,
model-spending cycle that must not be built on an unaudited anchor.

The anti-fabrication contract is the point. A metric that cannot be *shown* to
have a clean, fully-sized anchor — because its history is too short, because no
generation records a held-out set, or because the held-out set is smaller than
the paper's ten-item reference set — reports ``intact is None``, never ``True``.
An unaudited metric must never read as an intact one, and ``undecided`` names
the reason. Every rate, mean, and p-value over an unmeasured quantity is
``None``, never ``0.0``.

No DB, no lane, no network, no model, no numpy, no scipy: the only statistics
are a mean and an exact two-sided binomial sign test (``math.comb``).
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

MIN_GENERATIONS = 4      # below this, every test reports its n and a None p-value
ALPHA = 0.05
REFERENCE_SIZE = 10      # the paper's ten-item anchored reference set
DISCRIMINATION_FLOOR = 0.1   # at or below this, the metric is vacuous


def anchor_contaminated(row: Mapping) -> bool | None:
    """Whether the metric read its own held-out anchor.

    ``True`` when ``scored`` and ``held_out`` intersect — the metric read an id
    it declared held out, so the anchor is no longer held out. ``False`` when
    they are disjoint. ``None`` when either key is missing: a row without both
    keys carries no anchor measurement. A contaminated anchor is a fact about
    what was read, never an inference.
    """
    scored = row.get("scored")
    held_out = row.get("held_out")
    if scored is None or held_out is None:
        return None
    return bool(set(scored) & set(held_out))


def under_anchored(row: Mapping) -> bool | None:
    """Whether the held-out set is smaller than the paper's reference set.

    ``True`` when ``len(held_out) < REFERENCE_SIZE``; ``None`` when ``held_out``
    is missing. A metric whose anchor is smaller than ten items cannot be shown
    to carry the paper's anchored reference set.
    """
    held_out = row.get("held_out")
    if held_out is None:
        return None
    return len(held_out) < REFERENCE_SIZE


def agreement(row: Mapping) -> float | None:
    """Mean of ``agreements``, or ``None`` when empty or missing.

    Never ``0.0`` for an unmeasured agreement; ``0.0`` only for a measured
    all-disagree anchor.
    """
    agreements = row.get("agreements")
    if not agreements:
        return None
    return float(sum(agreements)) / len(agreements)


def discrimination(row: Mapping) -> float | None:
    """``min(p, 1 - p)`` where ``p`` is the fraction of ``agreements`` True.

    A metric that agreed with every anchor item has discrimination ``0.0``: it
    has stopped distinguishing anything. ``None`` when ``agreements`` is empty
    or missing.
    """
    agreements = row.get("agreements")
    if not agreements:
        return None
    p = float(sum(agreements)) / len(agreements)
    return min(p, 1.0 - p)


def _sign_test_p(pos: int, neg: int) -> float | None:
    """Exact two-sided binomial sign-test p at ``p = 0.5``.

    Summed over the tail with ``math.comb`` — no approximation, no scipy.
    ``None`` when ``pos + neg == 0``: a test in which every pair agrees carries
    no information.
    """
    n = pos + neg
    if n == 0:
        return None
    k = min(pos, neg)
    tail = sum(math.comb(n, i) for i in range(k + 1)) * (0.5 ** n)
    return min(1.0, 2.0 * tail)


def anchor_audit(history: Sequence[Mapping]) -> dict:
    """Anchor report over rows that carry an anchor measurement.

    ``n`` counts rows with both ``scored`` and ``held_out``; every other row
    carries no anchor measurement and is excluded. ``rate_contaminated`` is
    ``None`` (never ``0.0``) when ``n == 0``. ``contaminated`` is ``True`` iff
    ``n_contaminated > 0`` — ``False`` only when the anchor was actually
    measured and found clean.
    """
    measured = [row for row in history if anchor_contaminated(row) is not None]
    n = len(measured)
    n_contaminated = sum(1 for row in measured if anchor_contaminated(row) is True)
    n_under_anchored = sum(1 for row in measured if under_anchored(row) is True)
    return {
        "name": "anchor",
        "n": n,
        "n_contaminated": n_contaminated,
        "n_under_anchored": n_under_anchored,
        "rate_contaminated": (n_contaminated / n) if n else None,
        "contaminated": n_contaminated > 0,
    }


def _discrimination_series(history: Sequence[Mapping]) -> list[float]:
    """Usable per-generation discrimination values, in order.

    Rows whose discrimination is ``None`` are dropped — they carry no signal
    and are excluded from every test that reads that series.
    """
    values: list[float] = []
    for row in history:
        value = discrimination(row)
        if value is not None:
            values.append(value)
    return values


def vacuity(history: Sequence[Mapping]) -> dict:
    """Half-split of the discrimination series, and whether it is vacuous.

    First-half mean vs second-half mean (odd ``n`` gives the extra row to the
    second half), exactly the discipline of ``collapse_detector`` — not a
    fitted slope, which at small ``n`` is determined almost entirely by the
    endpoints. ``latest`` is the last usable discrimination; ``vacuous`` is
    whether it is at or below ``DISCRIMINATION_FLOOR``. ``p`` is the exact sign
    test over the non-zero per-step diffs.
    """
    values = _discrimination_series(history)
    n = len(values)
    if n < MIN_GENERATIONS:
        return {
            "name": "vacuity",
            "n": n,
            "first": None,
            "second": None,
            "delta": None,
            "latest": None,
            "vacuous": False,
            "p": None,
        }
    first = values[: n // 2]
    second = values[n // 2:]
    first_mean = sum(first) / len(first)
    second_mean = sum(second) / len(second)
    delta = second_mean - first_mean
    latest = values[-1]
    diffs = [b - a for a, b in zip(values, values[1:])]
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    p = _sign_test_p(pos, neg)
    return {
        "name": "vacuity",
        "n": n,
        "first": first_mean,
        "second": second_mean,
        "delta": delta,
        "latest": latest,
        "vacuous": latest is not None and latest <= DISCRIMINATION_FLOOR,
        "p": p,
    }


def intact(history: Sequence[Mapping]) -> bool | None:
    """The anti-fabrication verdict: is the anchor discipline intact?

    ``None`` (never ``True``) whenever the history has fewer than
    ``MIN_GENERATIONS`` rows carrying an anchor measurement — an unaudited
    metric must never read as an intact one. ``None`` also when the measured
    anchor is smaller than the reference set: the anchor cannot be certified.

    Otherwise ``False`` when the anchor is contaminated (the metric read its
    own anchor) or the discrimination has collapsed to the floor, and ``True``
    only when the anchor was measured, clean, fully-sized, and discriminating.
    """
    anchor = anchor_audit(history)
    vac = vacuity(history)
    if anchor["n"] < MIN_GENERATIONS:
        return None
    if anchor["contaminated"]:
        return False
    if anchor["n_under_anchored"] > 0:
        return None
    if vac["vacuous"]:
        return False
    return True


def _undecided_reasons(anchor: Mapping) -> list[str]:
    """Name every reason the audit could not conclude ``intact``."""
    reasons: list[str] = []
    if anchor["n"] == 0:
        reasons.append("no_anchor_measured")
    if anchor["n"] < MIN_GENERATIONS:
        reasons.append("insufficient_history")
    elif anchor["n_under_anchored"] > 0:
        reasons.append("under_anchored")
    return reasons


def audit(history: Sequence[Mapping]) -> dict:
    """One call: anchor report, vacuity report, and the intact verdict.

    ``undecided`` names every reason the audit could not conclude —
    ``"insufficient_history"``, ``"no_anchor_measured"``, ``"under_anchored"``
    — and is ``[]`` only when ``intact`` is ``True`` or ``False``. When
    ``intact`` is ``None``, ``undecided`` is non-empty.
    """
    materialized = list(history)
    anchor = anchor_audit(materialized)
    vac = vacuity(materialized)
    intact_value = intact(materialized)
    undecided = _undecided_reasons(anchor) if intact_value is None else []
    return {
        "n_generations": len(materialized),
        "n_with_anchor": anchor["n"],
        "anchor": anchor,
        "vacuity": vac,
        "intact": intact_value,
        "undecided": undecided,
    }
