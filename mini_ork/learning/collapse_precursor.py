"""Silent-collapse precursors over a generation history (arXiv 2605.14588).

``collapse_detector`` (G7) fires when the score rises *while* a frozen anchor
falls — a contradiction between two numbers the loop already watches. That is a
late signal: by the time the anchor has visibly dropped, the degradation is on
the record.

Silent Collapse in Recursive Learning Systems identifies something earlier and
quieter. Under recursive conditions a system's **internal distributions
contract while the standard metrics stay flat or improving** — the entropy of
an anchor outcome split collapses toward a degenerate all-pass (or all-fail)
state, and the coverage of tail scenarios erodes, several generations before
any visible metric moves. The internal signal fires first, the visible metric
moves later, and the gap between them is the warning window this module
measures.

The paper recurses on **weights**, so one of its three precursors — freezing of
representation drift — is a statement about a hidden-layer representation a
neural network carries and mini-ork does not. mini-ork recurses on harness and
prompt; it has no weights and no hidden representation to drift. That precursor
does not map, and this module says so in its vocabulary (``UNMAPPED``) rather
than inventing a placeholder that looks like a measurement. Two of three
precursors map; the module carries exactly those two and names the third as
unmapped. A detector that quietly covers two-thirds of a taxonomy and reports
it as the whole is the failure this file exists to avoid.

This is the *detector* half: it measures and reports lead time; it does not
act. The paper's MTR framework also regulates learning intensity off these
signals, and a regulator built on an unvalidated detector is the higher-stakes
cycle that must wait.

No DB, no lane, no network, no model, no numpy, no scipy: the caller assembles
the generation history; this module reads it. The only statistics are a Shannon
entropy over a two-way outcome count (``math.log2``) and an exact two-sided
binomial sign test (``math.comb``). Every rate, mean, and p-value over an
unmeasured quantity is ``None``, never ``0.0`` — an empty or short history must
never read as a healthy one, and a precursor that never fired must never read
as a precursor that fired early.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

PRECURSORS = ("anchor_entropy_contraction", "tail_coverage_erosion")
UNMAPPED = ("representation_drift_freezing",)  # weights-only; mini-ork has no weights
MIN_GENERATIONS = 4
MIN_TESTS = 2
ALPHA = 0.05


def anchor_entropy(row: Mapping) -> float | None:
    """Shannon entropy, base 2, of the anchor pass/fail split.

    ``-Σ p log2 p`` over the non-zero proportions. A degenerate all-pass (or
    all-fail) anchor has entropy ``0.0`` — it has stopped discriminating.
    ``None`` when either count is missing or the total is ``<= 0``.
    """
    anchor_pass = row.get("anchor_pass")
    anchor_fail = row.get("anchor_fail")
    if anchor_pass is None or anchor_fail is None:
        return None
    total = float(anchor_pass) + float(anchor_fail)
    if total <= 0:
        return None
    entropy = 0.0
    for count in (float(anchor_pass), float(anchor_fail)):
        if count > 0:
            p = count / total
            entropy -= p * math.log2(p)
    return entropy


def tail_coverage(row: Mapping) -> float | None:
    """``covered_tail / covered_total``, or ``None`` when coverage is unmeasured.

    A zero (or missing) ``covered_total`` carries no coverage measurement; an
    unmeasured coverage is ``None``, never ``0.0``.
    """
    covered_total = row.get("covered_total")
    if covered_total is None or covered_total <= 0:
        return None
    covered_tail = row.get("covered_tail")
    if covered_tail is None:
        return None
    return float(covered_tail) / float(covered_total)


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


def _series_fn(name: str):
    if name == "anchor_entropy_contraction":
        return anchor_entropy
    if name == "tail_coverage_erosion":
        return tail_coverage
    raise ValueError(f"unknown precursor: {name}")


def _series(history: Sequence[Mapping], name: str) -> list[float]:
    """Usable per-generation values for ``name``, in order.

    Rows whose measurement is ``None`` are dropped — they carry no signal and
    are excluded from every test that reads that series.
    """
    fn = _series_fn(name)
    values: list[float] = []
    for row in history:
        value = fn(row)
        if value is not None:
            values.append(value)
    return values


def precursor(history: Sequence[Mapping], name: str) -> dict:
    """Half-split mean of one precursor's series, and whether it is contracting.

    First-half mean vs second-half mean (odd ``n`` gives the extra row to the
    second half), exactly the discipline of ``collapse_detector`` — not a fitted
    slope, which at small ``n`` is determined almost entirely by the endpoints.
    ``contracting`` is ``delta < 0``: only a fall is contraction; a flat series
    is not. ``p`` is the exact sign test over the non-zero per-step diffs.
    """
    values = _series(history, name)  # raises ValueError on an unknown name
    n = len(values)
    if n < MIN_GENERATIONS:
        return {
            "name": name,
            "n": n,
            "first": None,
            "second": None,
            "delta": None,
            "contracting": False,
            "p": None,
        }
    first = values[: n // 2]
    second = values[n // 2:]
    first_mean = sum(first) / len(first)
    second_mean = sum(second) / len(second)
    delta = second_mean - first_mean
    diffs = [b - a for a, b in zip(values, values[1:])]
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    p = _sign_test_p(pos, neg)
    return {
        "name": name,
        "n": n,
        "first": first_mean,
        "second": second_mean,
        "delta": delta,
        "contracting": delta < 0,
        "p": p,
    }


def family_p(tests: Sequence[Mapping]) -> dict:
    """Šidák family-wise p over every non-``None`` per-test p.

    ``p_family = 1 - prod(1 - p_i)``. ``None`` when fewer than ``MIN_TESTS``
    tests contribute — one test is not a family.
    """
    contributing: list[str] = []
    prod = 1.0
    for test in tests:
        p = test.get("p")
        if p is not None:
            contributing.append(test.get("name", "?"))
            prod *= 1.0 - p
    k = len(contributing)
    p_family = (1.0 - prod) if k >= MIN_TESTS else None
    return {"k": k, "p_family": p_family, "contributing": contributing}


def standard_degradation_index(history: Sequence[Mapping]) -> int | None:
    """First ``gen`` at which the standard (``visible``) metric degrades.

    Degradation is the first ``gen`` whose ``visible`` is strictly below the
    running maximum of ``visible`` over all earlier usable rows. ``None`` when
    the metric never degrades, or when there is no earlier row to compare
    against.
    """
    running_max: float | None = None
    for i, row in enumerate(history):
        visible = row.get("visible")
        if visible is None:
            continue
        visible = float(visible)
        gen = row.get("gen", i)
        if running_max is None:
            running_max = visible
            continue
        if visible < running_max:
            return int(gen)
        if visible > running_max:
            running_max = visible
    return None


def precursor_onset(history: Sequence[Mapping], name: str) -> int | None:
    """First ``gen`` at which a precursor's local trend turns negative.

    The first ``gen`` whose value is strictly below the value at the previous
    usable generation. ``None`` when it never turns negative.
    """
    fn = _series_fn(name)
    prev: float | None = None
    for i, row in enumerate(history):
        value = fn(row)
        if value is None:
            continue
        gen = row.get("gen", i)
        if prev is not None and value < prev:
            return int(gen)
        prev = value
    return None


def lead_time(history: Sequence[Mapping]) -> dict:
    """Lead time: how many generations the precursor fired before the metric.

    ``lead = standard_gen - precursor_gen``, only when both are measured. A
    positive lead means the precursor fired first (the paper's claim); a
    negative lead means the precursor *trailed* the metric and is reported as
    the negative number it is, never clamped to zero. ``None`` when either
    index is unmeasured — an unmeasured lead is not a lead of ``0``.
    """
    materialized = list(history)
    onsets = [precursor_onset(materialized, name) for name in PRECURSORS]
    measured = [g for g in onsets if g is not None]
    precursor_gen = min(measured) if measured else None
    standard_gen = standard_degradation_index(materialized)
    if precursor_gen is not None and standard_gen is not None:
        lead = standard_gen - precursor_gen
    else:
        lead = None
    return {
        "precursor_gen": precursor_gen,
        "standard_gen": standard_gen,
        "lead": lead,
        "early_warning": lead is not None and lead > 0,
    }


def monitor(history: Sequence[Mapping]) -> dict:
    """One call: run the mapped precursors in fixed order and fold the report.

    ``precursors_unmapped`` is carried in the payload so a consumer cannot
    mistake two-of-three coverage for the whole taxonomy.
    """
    materialized = list(history)
    tests = [precursor(materialized, name) for name in PRECURSORS]
    return {
        "n_generations": len(materialized),
        "tests": tests,
        "family": family_p(tests),
        "lead": lead_time(materialized),
        "precursors_unmapped": list(UNMAPPED),
    }
