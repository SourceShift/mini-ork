"""Committee aggregation for behavioral verdicts (P3).

A *committee* is the set of verdicts produced by multiple behavioral
verifiers for the same target. :func:`committee_vote` returns a single
three-valued status following four invariants:

1. **REFUTED outranks** — any REFUTED verdict returns REFUTED immediately.
2. **Confidence-weighted totals** — PROVEN and UNVERIFIED totals are summed
   with caller-supplied weights (``1.0`` when absent).
3. **Strict majority** — the weighted PROVEN total must be *strictly greater*
   than the weighted UNVERIFIED total; an even tie is UNVERIFIED (abstain).
4. **Decorrelation guard** — PROVEN must come from at least two distinct
   non-empty surfaces. Multiple PROVEN votes from the same surface are
   correlated and cannot satisfy the guard.

:func:`pairwise_agreement` provides a separate inter-verdict agreement
score in ``[0.0, 1.0]`` (1.0 = unanimous, ``1.0`` when fewer than two
verdicts are supplied). Both helpers are pure functions of their inputs.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from mini_ork.verify.behavioral import (
    PROVEN,
    REFUTED,
    UNVERIFIED,
    BehavioralVerdict,
)

__all__ = ["committee_vote", "pairwise_agreement"]


_VALID_STATUSES = frozenset({PROVEN, REFUTED, UNVERIFIED})


def _normalize_surface(surface: str | None) -> str:
    """Surface name used for decorrelation.

    Empty / missing surfaces are all collapsed onto a single bucket so that
    PROVEN votes without a declared surface are not treated as distinct.
    """
    return (surface or "").strip() or ""


def pairwise_agreement(verdicts: Iterable[BehavioralVerdict]) -> float:
    """Fraction of status-equal pairs across all verdict pairs.

    Returns ``1.0`` when fewer than two verdicts are supplied (``nothing to
    disagree with`` — same convention as ``mini_ork.learning.eval_judge``).
    Result is bounded to ``[0.0, 1.0]``.
    """
    vs = list(verdicts)
    n = len(vs)
    if n < 2:
        return 1.0
    equal = 0
    total = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += 1
            if vs[i].status == vs[j].status:
                equal += 1
    return equal / total if total else 1.0


def _resolve_weights(
    verdicts: Sequence[BehavioralVerdict],
    weights: Sequence[float] | None,
) -> list[float]:
    """Return per-verdict weights, defaulting to ``1.0`` each.

    Raises :class:`ValueError` for any misalignment — silently treating a
    wrong-length or negative weight as ``1.0`` would corrupt the vote.
    """
    if weights is None:
        return [1.0] * len(verdicts)
    if len(weights) != len(verdicts):
        raise ValueError(
            f"weights length ({len(weights)}) must match verdicts length "
            f"({len(verdicts)})"
        )
    out: list[float] = []
    for w in weights:
        if not isinstance(w, (int, float)) or w < 0.0:
            raise ValueError(f"weights must be non-negative numbers, got {w!r}")
        out.append(float(w))
    return out


def committee_vote(
    verdicts: Iterable[BehavioralVerdict],
    weights: Sequence[float] | None = None,
) -> str:
    """Aggregate verdicts into one of PROVEN / REFUTED / UNVERIFIED.

    See module docstring for the four invariants. Empty input is UNVERIFIED
    (abstain). A weight length mismatch raises :class:`ValueError`.
    """
    vs = list(verdicts)
    if not vs:
        return UNVERIFIED

    for v in vs:
        if v.status not in _VALID_STATUSES:
            raise ValueError(f"verdict has unknown status: {v.status!r}")

    w = _resolve_weights(vs, weights)

    # Invariant 1: REFUTED outranks everything.
    if any(v.status == REFUTED for v in vs):
        return REFUTED

    proven_total = 0.0
    unverified_total = 0.0
    proven_surfaces: set[str] = set()
    for v, weight in zip(vs, w, strict=True):
        if v.status == PROVEN:
            proven_total += weight
            proven_surfaces.add(_normalize_surface(v.surface))
        elif v.status == UNVERIFIED:
            unverified_total += weight

    # Invariant 4: decorrelation — need at least two distinct non-empty
    # surfaces with PROVEN. An empty/unknown surface counts once.
    non_empty_surfaces = {s for s in proven_surfaces if s}
    if len(non_empty_surfaces) < 2:
        return UNVERIFIED

    # Invariant 3: strict majority for PROVEN over UNVERIFIED.
    if proven_total > unverified_total:
        return PROVEN
    return UNVERIFIED