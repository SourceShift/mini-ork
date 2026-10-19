"""Discrimination-anchored evidence for the solve-time oracle.

The oracle validates a reproduction test against **one** known-correct patch
(the PoC+ is green on the fix, red on base). That says the test exercises the
bug; it says nothing about whether the test can separate a correct patch from a
plausible wrong one. A coverage-gap cheat passes both the PoC+ and the
metamorphic term when the invariants happen to share the probe's blind spot.

This module adds the missing measurement: given a candidate pool with
known-correct and known-bad patches, run the reproduction test against each and
score how well it separates them. The result is *evidence quality*, not a patch
verdict — `REFUTED` here means "this test cannot tell right from wrong", which
is why the oracle downgrades a would-be PROVEN to UNVERIFIED rather than
returning REFUTED.

Semantics that matter:

  - ``passed`` = accept, ``failed`` = reject.
  - ``error``/``test_defect``/``apply_fail``/``no_run`` (and any unknown
    status) = **unmeasured**: excluded from numerator *and* denominator. A
    harness fact is not evidence about the test's discriminating power; counting
    it as a rejection would silently depress the score.
  - A score is only claimed when BOTH classes are measured (``measured > 0``,
    ``correct_total > 0``, ``incorrect_total > 0``). One class alone cannot
    demonstrate separation.
  - Deterministic for a deterministic runner; no model call.

Knob: ``MO_DISCRIMINATION_MIN`` (default ``DEFAULT_MIN_DISCRIMINATION`` = 1.0).
Values outside [0, 1] are rejected back to the default.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

PROVEN, REFUTED, UNVERIFIED = "PROVEN", "REFUTED", "UNVERIFIED"

DEFAULT_MIN_DISCRIMINATION = 1.0

#: Statuses that describe the HARNESS, not the test's discriminating power.
UNMEASURED_STATUSES = frozenset({"error", "test_defect", "apply_fail", "no_run"})


@dataclass(frozen=True)
class Candidate:
    """One patch in the discrimination pool, labelled from corpus construction."""

    patch: str
    correct: bool


@dataclass(frozen=True)
class DiscriminationResult:
    """Outcome of scoring one reproduction test against a candidate pool.

    ``checks`` is three-valued per candidate: ``ok=True`` the candidate behaved
    as its label requires, ``ok=False`` it did not, ``ok=None`` it was
    unmeasured. ``correct_total``/``incorrect_total`` count the pool's usable
    candidates per class (an unmeasured entry still proves its class exists);
    the score denominator is the measured count, ``good / measured``.
    """

    status: str
    discrimination: float | None
    correct_accepted: int
    correct_total: int
    incorrect_rejected: int
    incorrect_total: int
    checks: list[dict]
    evidence: str


def _resolve_min(min_discrimination: float | None) -> float:
    """Effective minimum, tighten-only: out-of-range values fall back to default.

    An explicit argument wins over the env knob, but both pass through the same
    range check — a misconfigured ``MO_DISCRIMINATION_MIN=1.5`` must not demand
    more than perfect separation, and ``-0.25`` must not make anything pass.
    """
    raw: object = (
        min_discrimination
        if min_discrimination is not None
        else os.environ.get("MO_DISCRIMINATION_MIN", DEFAULT_MIN_DISCRIMINATION)
    )
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_MIN_DISCRIMINATION
    if not 0.0 <= value <= 1.0:  # also rejects NaN
        return DEFAULT_MIN_DISCRIMINATION
    return value


def score(
    test_src: str,
    candidates: list[Candidate],
    *,
    runner,
    min_discrimination: float | None = None,
) -> DiscriminationResult:
    """Score `test_src` by how well it separates correct from incorrect patches.

    `runner` is the caller-owned duck-typed runner (``.run_test(src, patch=...)``
    returning an object with ``.status``); nothing is constructed here.
    """
    threshold = _resolve_min(min_discrimination)

    checks: list[dict] = []
    correct_accepted = correct_total = 0
    incorrect_rejected = incorrect_total = 0
    measured = 0
    rejected_correct: list[str] = []
    accepted_incorrect: list[str] = []

    for candidate in candidates or []:
        patch = getattr(candidate, "patch", None)
        correct = getattr(candidate, "correct", None)
        if not isinstance(patch, str) or not isinstance(correct, bool):
            continue
        outcome = runner.run_test(test_src, patch=patch)
        status = getattr(outcome, "status", "error")
        kind = "correct" if correct else "incorrect"
        # Class totals count the POOL, so an unmeasured entry still proves the
        # class exists; only the score denominator is measured-only.
        if correct:
            correct_total += 1
        else:
            incorrect_total += 1

        if status == "passed":
            good = correct  # accepting a correct patch is good; accepting a bad one is not
            check = {"name": f"{kind}:{patch}", "ok": good,
                     "detail": "accepted (status=passed)"}
        elif status == "failed":
            good = not correct  # rejecting a bad patch is good; rejecting a correct one is not
            check = {"name": f"{kind}:{patch}", "ok": good,
                     "detail": "rejected (status=failed)"}
        else:
            # error / test_defect / apply_fail / no_run / anything unrecognised:
            # unmeasured, never a rejection.
            checks.append({"name": f"{kind}:{patch}", "ok": None,
                           "detail": f"unmeasured (status={status})"})
            continue

        checks.append(check)
        measured += 1
        if correct:
            if good:
                correct_accepted += 1
            else:
                rejected_correct.append(patch)
        else:
            if good:
                incorrect_rejected += 1
            else:
                accepted_incorrect.append(patch)

    if measured == 0:
        return _unmeasurable("no candidate run produced a measured outcome", checks)
    if correct_total == 0:
        return _unmeasurable("no known-correct candidate in the pool", checks)
    if incorrect_total == 0:
        return _unmeasurable("no known-bad candidate in the pool", checks)

    good = correct_accepted + incorrect_rejected
    discrimination = good / measured

    if discrimination >= threshold:
        return DiscriminationResult(
            status=PROVEN,
            discrimination=discrimination,
            correct_accepted=correct_accepted,
            correct_total=correct_total,
            incorrect_rejected=incorrect_rejected,
            incorrect_total=incorrect_total,
            checks=checks,
            evidence=(f"discrimination {discrimination:.3f} >= {threshold:.3f} "
                      f"({good}/{measured} candidates separated)"),
        )

    witnesses = (f"known-correct rejected: {sorted(rejected_correct)}; "
                 f"known-bad accepted: {sorted(accepted_incorrect)}")
    return DiscriminationResult(
        status=REFUTED,
        discrimination=discrimination,
        correct_accepted=correct_accepted,
        correct_total=correct_total,
        incorrect_rejected=incorrect_rejected,
        incorrect_total=incorrect_total,
        checks=checks,
        evidence=(f"discrimination {discrimination:.3f} < {threshold:.3f} "
                  f"({good}/{measured} candidates separated); {witnesses}"),
    )


def _unmeasurable(reason: str, checks: list[dict]) -> DiscriminationResult:
    """A score is never claimed from one class alone, or from no measurements."""
    return DiscriminationResult(
        status=UNVERIFIED,
        discrimination=None,
        correct_accepted=sum(1 for c in checks
                             if c["ok"] is True and c["name"].startswith("correct:")),
        correct_total=sum(1 for c in checks if c["name"].startswith("correct:")),
        incorrect_rejected=sum(1 for c in checks
                               if c["ok"] is True and c["name"].startswith("incorrect:")),
        incorrect_total=sum(1 for c in checks if c["name"].startswith("incorrect:")),
        checks=checks,
        evidence=f"discrimination unmeasurable: {reason}",
    )


def enabled() -> bool:
    """True iff ``MO_ASSAY_DISCRIMINATION`` is truthy. DEFAULT ON; ``0``/``off`` disables."""
    value = os.environ.get("MO_ASSAY_DISCRIMINATION", "1")
    return value.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "DEFAULT_MIN_DISCRIMINATION",
    "PROVEN",
    "REFUTED",
    "UNMEASURED_STATUSES",
    "UNVERIFIED",
    "Candidate",
    "DiscriminationResult",
    "enabled",
    "score",
]
