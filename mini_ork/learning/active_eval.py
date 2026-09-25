"""Coverage-aware selection of the next scenario to probe (arXiv 2608.13719).

A gate's blind-spot rate is measured over a corpus someone else chose. The
question a fuzzer cannot answer is which scenario to probe *next*: the corpus
does not grow toward the failures that matter, so the gate can be blind in a
region no probe ever visits. "Coverage Aware Active Evaluation" turns that
selection into a module with two claims.

The first claim is the anti-fabrication contract. A cheap proxy — here, a
frozen evaluator run against a scenario that never reaches a real lane — is
not the target. Proxy failures often do not transfer to the real system
(sim-to-real and system-to-system gaps), so a proxy pass is **not** evidence of
a real pass, and a selector that ranks on the proxy alone is selecting on the
unaudited thing. The correction is a *control-variate residual*: over the
scenarios where both the proxy and the target were observed, how far off is the
proxy? With no such paired observations the residual is ``None`` and **every**
derived risk is ``None`` — the module refuses to recommend rather than fall
back to the proxy.

The second claim is the acquisition objective: risk alone collapses selection
onto one failure mode. A candidate is worth probing only if it is *likely* to
fail **and** it covers a mode the already-selected set does not — gated by
whether that region is realistic at all (``support``).

This module builds the *selection* half. It measures which scenario to probe
next; it does not generate scenarios, run a lane, call a model, or edit the
corpus. No DB, no lane, no network, no model, no numpy, no scipy: the only
statistics are a mean and an exact two-sided binomial sign test (``math.comb``).
Every rate, mean, and risk over an unmeasured quantity is ``None``, never
``0.0``.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

MIN_PAIRED = 4        # below this, the transfer residual is None
MIN_CANDIDATES = 4    # below this, the selection is insufficient
ALPHA = 0.05
SUPPORT_FLOOR = 0.1   # at or below this, the region is not well-supported


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


def _clamp01(value: float) -> float:
    """Clamp ``value`` into ``[0.0, 1.0]``."""
    return max(0.0, min(1.0, value))


def _as_float(flag: bool | None) -> float:
    """``True`` -> ``1.0``, ``False`` -> ``0.0``."""
    return 1.0 if flag else 0.0


def proxy_rate(rows: Sequence[Mapping]) -> float | None:
    """Mean of ``proxy_failed`` over rows that measured it; ``None`` when none did.

    Never ``0.0`` for an unmeasured rate; ``0.0`` only for a measured
    all-pass proxy.
    """
    vals = [r["proxy_failed"] for r in rows if r.get("proxy_failed") is not None]
    if not vals:
        return None
    return float(sum(vals)) / len(vals)


def target_rate(rows: Sequence[Mapping]) -> float | None:
    """Mean of ``target_failed`` over rows that measured it; ``None`` when none did."""
    vals = [r["target_failed"] for r in rows if r.get("target_failed") is not None]
    if not vals:
        return None
    return float(sum(vals)) / len(vals)


def paired(rows: Sequence[Mapping]) -> list[Mapping]:
    """Rows where **both** ``proxy_failed`` and ``target_failed`` are present and non-``None``.

    This is the only evidence that can correct the proxy.
    """
    return [
        r for r in rows
        if r.get("proxy_failed") is not None and r.get("target_failed") is not None
    ]


def transfer_residual(rows: Sequence[Mapping]) -> float | None:
    """Control-variate correction: mean of ``target_failed - proxy_failed`` over paired rows.

    ``True`` is ``1.0``, ``False`` is ``0.0``. ``None`` when there are fewer
    than ``MIN_PAIRED`` paired rows. A **positive** residual means the proxy
    under-reports failure (proxy said pass while the target failed) — the
    dangerous direction. Never ``None`` rendered as ``0.0``.
    """
    pr = paired(rows)
    if len(pr) < MIN_PAIRED:
        return None
    diffs = [
        _as_float(r["target_failed"]) - _as_float(r["proxy_failed"]) for r in pr
    ]
    return sum(diffs) / len(diffs)


def residual_p(rows: Sequence[Mapping]) -> float | None:
    """Exact two-sided sign-test p over the sign of the paired differences.

    ``pos`` counts positive residuals (``target_failed - proxy_failed > 0``),
    ``neg`` counts negative ones; a zero difference is dropped. ``None`` when
    there are no non-zero differences. Reported, never used to gate the
    residual — the caller decides.
    """
    pos = 0
    neg = 0
    for r in paired(rows):
        d = _as_float(r["target_failed"]) - _as_float(r["proxy_failed"])
        if d > 0:
            pos += 1
        elif d < 0:
            neg += 1
    return _sign_test_p(pos, neg)


def predicted_risk(row: Mapping, residual: float | None) -> float | None:
    """``clamp01(proxy_failed + residual)`` for a row that measured ``proxy_failed``.

    ``None`` when ``residual is None`` **or** the row's ``proxy_failed`` is
    missing — an uncorrected proxy risk is not a risk. The result is always
    clamped into ``[0.0, 1.0]``.
    """
    if residual is None:
        return None
    pf = row.get("proxy_failed")
    if pf is None:
        return None
    return _clamp01(_as_float(pf) + residual)


def coverage_gain(candidate: Mapping, selected: Sequence[Mapping]) -> float | None:
    """Count of modes in ``candidate`` that appear in **no** row of ``selected``.

    ``None`` when the candidate's ``modes`` or ``support`` is missing. ``0.0``
    when ``support <= SUPPORT_FLOOR`` even if it covers new modes: the paper's
    objective favors realistic, well-supported regions, so a mode never observed
    at a realistic density is not worth expanding into.
    """
    modes = candidate.get("modes")
    support = candidate.get("support")
    if modes is None or support is None:
        return None
    if support <= SUPPORT_FLOOR:
        return 0.0
    seen: set = set()
    for row in selected:
        m = row.get("modes")
        if m:
            seen.update(m)
    return float(sum(1 for mode in modes if mode not in seen))


def acquisition(
    candidate: Mapping, selected: Sequence[Mapping], residual: float | None
) -> float | None:
    """The selection objective: ``predicted_risk * coverage_gain``.

    ``None`` when **either** factor is ``None``; a candidate with unknown risk
    or unknown coverage is not scored, never scored as ``0.0``. A candidate
    with a known risk but zero coverage gain scores ``0.0`` — a measured zero,
    and how risk-only collapse is prevented.
    """
    risk = predicted_risk(candidate, residual)
    gain = coverage_gain(candidate, selected)
    if risk is None or gain is None:
        return None
    return risk * gain


def rank(history: Sequence[Mapping], budget: int) -> dict:
    """Greedy selection of the next ``budget`` scenarios to probe.

    Repeatedly take the unselected candidate with the highest ``acquisition``
    against the current ``selected`` set (ties broken by ``id`` ascending, for
    determinism), append it, and recompute; stop early when no remaining
    candidate has an acquisition ``> 0.0`` or all are ``None``.

    ``selected`` holds only ids with a non-``None`` acquisition, at most
    ``budget``. ``unmeasured`` holds the ids whose acquisition is ``None`` —
    scenarios the proxy cannot speak to; they are reported, never quietly
    ranked as safe. ``undecided`` names every reason the ranking could not
    proceed and is ``[]`` only when ``selected`` is non-empty.
    """
    materialized = [dict(r) for r in history]
    n_candidates = len(materialized)
    residual = transfer_residual(materialized)

    selected_rows: list[Mapping] = []
    remaining = list(materialized)

    while len(selected_rows) < budget:
        best_row = None
        best_acq = None
        for row in remaining:
            acq = acquisition(row, selected_rows, residual)
            if acq is None:
                continue
            if (
                best_acq is None
                or acq > best_acq
                or (acq == best_acq and row["id"] < best_row["id"])
            ):
                best_acq = acq
                best_row = row
        if best_row is None or best_acq <= 0.0:
            break
        selected_rows.append(best_row)
        remaining = [r for r in remaining if r["id"] != best_row["id"]]

    selected = [row["id"] for row in selected_rows]
    unmeasured = [
        row["id"] for row in materialized if acquisition(row, [], residual) is None
    ]

    if selected:
        undecided: list[str] = []
    else:
        undecided = []
        if residual is None:
            undecided.append("insufficient_paired")
        if n_candidates < MIN_CANDIDATES:
            undecided.append("insufficient_candidates")
        if not any(
            coverage_gain(row, []) is not None and coverage_gain(row, []) > 0.0
            for row in materialized
        ):
            undecided.append("no_coverage")

    return {
        "name": "rank",
        "n_candidates": n_candidates,
        "budget": budget,
        "selected": selected,
        "unmeasured": unmeasured,
        "undecided": undecided,
    }


def report(history: Sequence[Mapping], budget: int = 1) -> dict:
    """One call: proxy/target rates, the residual, the ranking, and any ``undecided``.

    ``proxy`` and ``target`` carry their rate plus their ``n``. ``undecided`` is
    the union of the rank's ``undecided`` and ``"no_transfer_measured"`` when
    the residual is ``None``. It is ``[]`` **only** when the residual was
    measured and the rank selected a candidate.
    """
    materialized = list(history)
    n_rows = len(materialized)
    pr = paired(materialized)
    residual = transfer_residual(materialized)
    rank_result = rank(materialized, budget)

    undecided = list(rank_result["undecided"])
    if residual is None:
        undecided.append("no_transfer_measured")

    proxy_n = sum(1 for r in materialized if r.get("proxy_failed") is not None)
    target_n = sum(1 for r in materialized if r.get("target_failed") is not None)

    return {
        "n_rows": n_rows,
        "n_paired": len(pr),
        "proxy": {"n": proxy_n, "rate": proxy_rate(materialized)},
        "target": {"n": target_n, "rate": target_rate(materialized)},
        "transfer_residual": residual,
        "rank": rank_result,
        "undecided": undecided,
    }
