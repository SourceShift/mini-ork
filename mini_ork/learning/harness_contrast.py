"""Hermetic paired-contrast attribution for an A-vs-B harness claim.

A *harness contrast* is a set of paired rows — one per probe — where the same
input ran through harness A and harness B on ONE lane, and each row records
whether each harness passed. ``attribute`` turns those rows into an attribution
report: by how much B's pass rate exceeds A's, split into B-only and A-only
wins, and whether the contrast is *resolved*.

The core discipline is the lane control (arXiv 2609.11987): a differential
claim between two harnesses is meaningful only when every pair ran on the SAME
lane, so a stronger model cannot be filed as a better harness. A set that mixes
lanes, or a row with no lane at all, is contamination — ``attribute`` **raises**
rather than reporting a number. Contamination that raises is a control;
contamination that reports is a bug.

``resolved`` is ``discordant > 0``: a contrast in which every row agrees
carries no information about the difference between the harnesses, however many
rows there are. The delta may read ``0.0`` and still be an absence of evidence,
not evidence of absence — which is what stops a loop promoting on an all-agree
run.

Rates and deltas over an empty set are ``None``, never ``0.0``: an unmeasured
quantity is not a zero. A short (or empty) history must never read as a healthy
one.

This module is a pure function over caller-produced rows. It performs no I/O,
no live runs and no lane wiring — ``probe_scorer`` remains the single-lineage
scorer; this is its paired sibling, not a replacement.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping


def attribute(rows: Iterable[Mapping]) -> dict:
    """Attribute an A-vs-B contrast to the harness, or refuse on contamination.

    Each row is a mapping with at least ``{"probe": str, "lane": str, "a": bool,
    "b": bool}`` where ``a``/``b`` mean "passed under harness A / B".

    Report shape::

        {"n", "lane", "lane_fixed", "a_pass", "b_pass", "a_rate", "b_rate",
         "delta", "resolved", "discordant", "a_only", "b_only",
         "rows": [{"probe", "a", "b", "delta", "kind"}]}

    ``a_rate`` / ``b_rate`` / ``delta`` are ``None`` when ``n == 0``.
    ``resolved`` is ``discordant > 0``. Rows are echoed in input order with
    ``delta`` (``+1`` for b-only, ``-1`` for a-only, ``0`` for agree) and
    ``kind`` (``agree-pass`` / ``agree-fail`` / ``a-only`` / ``b-only``) so a
    caller can audit which probes carried the difference.

    Raises ``ValueError`` when any row has an empty/missing lane (naming the
    probe), or when the rows mix lanes (naming the offending probe and both
    lane values).
    """
    materialized = list(rows)
    if not materialized:
        return {
            "n": 0,
            "lane": "",
            "lane_fixed": True,
            "a_pass": 0,
            "b_pass": 0,
            "a_rate": None,
            "b_rate": None,
            "delta": None,
            "resolved": False,
            "discordant": 0,
            "a_only": 0,
            "b_only": 0,
            "rows": [],
        }

    lane = ""
    a_pass = 0
    b_pass = 0
    a_only = 0
    b_only = 0
    echoed: list[dict] = []

    for row in materialized:
        probe = row.get("probe", "")
        row_lane = row.get("lane")
        if not isinstance(row_lane, str) or not row_lane.strip():
            raise ValueError(f"row {probe!r} has an empty lane")
        if not lane:
            lane = row_lane
        elif row_lane != lane:
            raise ValueError(
                f"row {probe!r} is on lane {row_lane!r} but the other rows are "
                f"on lane {lane!r} — a mixed-lane contrast cannot be attributed"
            )

        a = bool(row.get("a"))
        b = bool(row.get("b"))
        a_pass += int(a)
        b_pass += int(b)

        if a and b:
            kind, delta = "agree-pass", 0
        elif not a and not b:
            kind, delta = "agree-fail", 0
        elif a:
            kind, delta = "a-only", -1
            a_only += 1
        else:
            kind, delta = "b-only", 1
            b_only += 1

        echoed.append({"probe": probe, "a": a, "b": b, "delta": delta, "kind": kind})

    n = len(materialized)
    discordant = a_only + b_only
    return {
        "n": n,
        "lane": lane,
        "lane_fixed": True,
        "a_pass": a_pass,
        "b_pass": b_pass,
        "a_rate": a_pass / n,
        "b_rate": b_pass / n,
        "delta": (b_pass - a_pass) / n,
        "resolved": discordant > 0,
        "discordant": discordant,
        "a_only": a_only,
        "b_only": b_only,
        "rows": echoed,
    }


def _fmt(value: float | None, spec: str = ".3f") -> str:
    return "-" if value is None else format(value, spec)


def summarize(report: Mapping) -> str:
    """Render the contrast as a short human block.

    ``None`` rates and deltas render as ``-``. An unresolved contrast carries
    the literal marker ``unresolved (no discordant pairs)``.
    """
    verdict = (
        f"resolved ({report['discordant']} discordant)"
        if report["resolved"]
        else "unresolved (no discordant pairs)"
    )
    return (
        f"harness contrast on lane {report['lane'] or '-'} (n={report['n']})\n"
        f"  A pass rate: {_fmt(report['a_rate'])}\n"
        f"  B pass rate: {_fmt(report['b_rate'])}\n"
        f"  delta (B - A): {_fmt(report['delta'], '+.3f')}\n"
        f"  a-only: {report['a_only']}   b-only: {report['b_only']}   "
        f"discordant: {report['discordant']}\n"
        f"  {verdict}"
    )
