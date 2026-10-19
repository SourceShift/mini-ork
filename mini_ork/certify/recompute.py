"""Execution-anchored recomputation: fold a declared input log through the subject.

The other behavioral surfaces check a *single* observation. A stateful subject
can return the right answer from stale or fabricated state, so this module
replays a declared record log through the subject's own ``apply_fn`` and
compares the resulting **functional state** — two independent replays (a
determinism probe) plus, when declared, the expected final state. The internal
trace is evidence, never a pass criterion: :func:`check` compares only final
states (and the declared expectation), while :func:`replay` exposes per-step
states to callers/tests.

Three-valued discipline, same as :mod:`mini_ork.verify.behavioral`:

    PROVEN     — two replays agree and any declared expectation holds.
    REFUTED    — the replays diverge, or the declared expectation does not hold.
    UNVERIFIED — the check could not be evaluated (no records, an ``apply_fn``
                 that raised, a state that is not canonical JSON, or an
                 equivalence operator that cannot be evaluated). A harness
                 failure is never evidence against the subject.

``apply_fn(state, record)`` must be pure with respect to its inputs and must be
deterministic: ``copy.deepcopy``-able state is the caller's contract. Records
are plain JSON values.

Import-time contract: pure stdlib (plus the stdlib-only
:mod:`mini_ork.verify.equivalence`). Canonicalization here is deliberately
strict — no ``default=``/``repr`` fallback — so an opaque final state makes the
check abstain instead of comparing an unrepresentable object.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Callable

from mini_ork.verify.equivalence import EquivalenceSpec, compare, spec_error

__all__ = [
    "PROVEN",
    "REFUTED",
    "UNVERIFIED",
    "RecomputeResult",
    "replay",
    "check",
]

PROVEN = "PROVEN"
REFUTED = "REFUTED"
UNVERIFIED = "UNVERIFIED"

ApplyFn = Callable[[Any, Any], Any]


@dataclass(frozen=True)
class RecomputeResult:
    """Outcome of one recomputation.

    ``checks`` holds ``{"name", "ok", "detail"}`` entries in the same shape the
    behavioral verifier emits. ``witness`` is ``None`` unless a refutation pins
    a concrete step: ``{"index": int, "left": <canonical>, "right": <canonical>}``.
    """

    status: str
    checks: list[dict]
    evidence: str
    witness: dict | None = None


def _canonical(value: Any) -> str:
    """Strict canonical JSON text — sorted keys, no opaque-object fallback.

    Anything not representable as JSON raises; the caller maps that to an
    abstention because equality of an unrepresentable state cannot be
    established.
    """
    return json.dumps(value, sort_keys=True)


def _evidence(status: str, checks: list[dict]) -> str:
    detail = "; ".join(
        f"{entry['name']}: {entry['detail']}" for entry in checks
    )
    return f"{status}: {detail}" if detail else status


def _abstain(name: str, detail: str) -> RecomputeResult:
    checks = [{"name": name, "ok": None, "detail": detail}]
    return RecomputeResult(UNVERIFIED, checks, _evidence(UNVERIFIED, checks), None)


def replay(
    apply_fn: ApplyFn,
    records: list,
    *,
    initial_state: Any = None,
) -> list[Any]:
    """Fold ``records`` left-to-right through ``apply_fn``.

    ``initial_state`` is deep-copied first, so the caller's object is never
    mutated in place, and each step becomes ``state = apply_fn(state, record)``.
    Returns the state after every record (empty when ``records`` is empty).
    """
    state = copy.deepcopy(initial_state)
    states = []
    for record in records:
        state = apply_fn(state, record)
        states.append(state)
    return states


def _first_divergence(left: list, right: list) -> tuple[int, str, str]:
    """Return the first step whose canonical forms differ, plus both forms."""
    for index, (left_state, right_state) in enumerate(zip(left, right)):
        left_text = _canonical(left_state)
        right_text = _canonical(right_state)
        if left_text != right_text:
            return index, left_text, right_text
    # The finals already differ, so the last step differs; this is unreachable
    # for equal-length replays, but keeps the contract total.
    index = len(left) - 1
    return index, _canonical(left[index]), _canonical(right[index])


def check(
    apply_fn: ApplyFn,
    records: list,
    *,
    initial_state: Any = None,
    expected_state: Any = None,
    has_expected: bool = False,
    equivalence: str = "exact",
) -> RecomputeResult:
    """Replay ``records`` twice and compare the final functional states.

    ``has_expected`` guards the expectation: ``expected_state`` is only a pass
    criterion when it is explicitly declared. Comparison of the final state
    against ``expected_state`` delegates to the shipped
    :func:`mini_ork.verify.equivalence.compare` operator for ``exact``/``set``/
    ``tolerant``; an operator that cannot evaluate (``equal is None``) abstains.
    """
    if not records:
        return _abstain("records", "no recompute records declared")

    label = str(equivalence or "exact")
    spec = EquivalenceSpec(operator=label)
    spec_err = spec_error(spec)
    if spec_err:
        return _abstain("equivalence", spec_err)

    try:
        first = replay(apply_fn, records, initial_state=initial_state)
        second = replay(apply_fn, records, initial_state=initial_state)
    except Exception as exc:  # noqa: BLE001 — a harness failure must abstain
        return _abstain(
            "replay", f"apply raised {type(exc).__name__}: {exc}"
        )

    try:
        first_final = _canonical(first[-1])
        second_final = _canonical(second[-1])
    except Exception as exc:  # noqa: BLE001 — unrepresentable state abstains
        return _abstain(
            "canonical_state",
            f"final state is not canonical JSON: {type(exc).__name__}: {exc}",
        )

    if first_final != second_final:
        try:
            index, left, right = _first_divergence(first, second)
        except Exception as exc:  # noqa: BLE001 — witness unknown, so abstain
            return _abstain(
                "canonical_state",
                f"per-step state is not canonical JSON: {type(exc).__name__}: {exc}",
            )
        checks = [{
            "name": "determinism",
            "ok": False,
            "detail": f"replays diverge at step {index}: {left} != {right}",
        }]
        return RecomputeResult(
            REFUTED,
            checks,
            _evidence(REFUTED, checks),
            {"index": index, "left": left, "right": right},
        )

    checks = [{
        "name": "determinism",
        "ok": True,
        "detail": "two independent replays agree on the final state",
    }]

    if has_expected:
        outcome = compare(first[-1], expected_state, spec)
        if outcome.equal is None:
            checks.append({
                "name": "expected_state",
                "ok": None,
                "detail": outcome.detail or "comparison could not be evaluated",
            })
            return RecomputeResult(UNVERIFIED, checks, _evidence(UNVERIFIED, checks), None)
        if outcome.equal is False:
            try:
                expected_text = _canonical(expected_state)
            except Exception as exc:  # noqa: BLE001 — cannot pin a witness
                checks.append({
                    "name": "expected_state",
                    "ok": None,
                    "detail": (
                        "expect_state is not canonical JSON: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                })
                return RecomputeResult(
                    UNVERIFIED, checks, _evidence(UNVERIFIED, checks), None
                )
            checks.append({
                "name": "expected_state",
                "ok": False,
                "detail": outcome.detail or f"final state != expect_state under '{label}'",
            })
            return RecomputeResult(
                REFUTED,
                checks,
                _evidence(REFUTED, checks),
                {
                    "index": len(records) - 1,
                    "left": expected_text,
                    "right": first_final,
                },
            )
        checks.append({
            "name": "expected_state",
            "ok": True,
            "detail": f"final state equivalent to expect_state under '{label}'",
        })

    return RecomputeResult(PROVEN, checks, _evidence(PROVEN, checks), None)
