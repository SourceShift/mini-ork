"""Run-time assurance (RTA) shield for the goal-loop's outer driver.

A shield is a predicate that sits between a controller and the plant it drives
and refuses any command that would violate an invariant the controller is not
trusted to respect on its own. Here the "plant" is a book and the "controller"
is an outer loop whose only actuator is spawning a fix child and deploying what
it returns.

Three invariants, each one a bug this loop has actually committed:

``budget``
    Never start a wave whose projected cost crosses the cumulative ceiling.
    The driver already computes this, but as an inline branch it is invisible
    to any consumer of the loop's decisions; as a guard it is one recorded
    verdict among the others.

``destructive``
    Never take an action that regenerates a chapter destructively. An anchor
    markdown is a chapter's only copy, so a destructive re-drive that fails
    leaves nothing behind. A destructive action is refused unless the context
    says it was authorized explicitly — the shield's default is "preserve".

``stale-evidence``
    Never re-issue a byte-identical evidence bundle for a unit whose predicate
    did not move on the previous identical bundle. This is the progress mirage
    in its actionable form: the loop is not merely wasting a wave, it is
    re-running a wave whose input is provably unchanged, so the output cannot
    differ.

The shield is PURE: it reads two dicts and returns a verdict. It performs no
I/O, cannot mutate its inputs, and has no opinion about whether it should be
obeyed — ``resolve_mode`` and the caller decide that.

Default mode is ``shadow``: the verdict is computed and recorded on every wave
and changes nothing. ``MO_GOAL_SHIELD=enforce`` lets a refusal stop the loop;
``MO_GOAL_SHIELD=off`` skips evaluation entirely. Shadow-by-default is
deliberate — a shield whose guards have not been observed against a real run
is a guess, and a wrong refusal on a healthy wave is indistinguishable from a
correct one until the logs exist to tell them apart.
"""
from __future__ import annotations

import os
from typing import Any, Callable

MODE_ENV = "MO_GOAL_SHIELD"
MODE_DEFAULT = "shadow"
MODES: tuple[str, ...] = ("off", "shadow", "enforce")


def resolve_mode(value: str | None = None) -> str:
    """Resolve the shield mode; anything unrecognised falls back to the default.

    An operator typo (``MO_GOAL_SHIELD=enforce!``) must not silently disable a
    safety surface, and must not silently arm one either — it lands on
    ``shadow``, which is the mode that measures without acting.
    """
    if value is None:
        value = os.environ.get(MODE_ENV, "")
    resolved = (value or "").strip().lower()
    return resolved if resolved in MODES else MODE_DEFAULT


def _budget_guard(_action: dict[str, Any], context: dict[str, Any]) -> str | None:
    budget = context.get("budget_total_usd")
    if not isinstance(budget, (int, float)) or budget <= 0:
        return None
    spent = float(context.get("spent_usd") or 0.0)
    projected = float(context.get("projected_wave_usd") or 0.0)
    if spent >= budget:
        return f"spent=${spent:.2f} already >= budget=${budget:.2f}"
    if projected > 0 and (spent + projected) > budget:
        return (
            f"spent=${spent:.2f} + projected=${projected:.2f} "
            f"> budget=${budget:.2f}"
        )
    return None


def _destructive_guard(action: dict[str, Any], context: dict[str, Any]) -> str | None:
    if not action.get("destructive"):
        return None
    if context.get("destructive_authorized"):
        return None
    units = ",".join(sorted(str(u) for u in (action.get("units") or [])))
    return (
        f"action {action.get('kind')!r} would destructively regenerate "
        f"unit(s) {units or '<none>'} — an anchor markdown is a chapter's only copy"
    )


def _stale_evidence_guard(action: dict[str, Any], context: dict[str, Any]) -> str | None:
    if context.get("predicate_moved"):
        return None
    this_ev = context.get("evidence_sha") or {}
    prev_ev = context.get("prev_evidence_sha") or {}
    if not this_ev or not prev_ev:
        return None
    stale = sorted(
        str(unit)
        for unit in (action.get("units") or [])
        if this_ev.get(str(unit)) and this_ev.get(str(unit)) == prev_ev.get(str(unit))
    )
    if not stale:
        return None
    return (
        "evidence bundle byte-identical across two waves for unit(s) "
        + ",".join(stale)
        + " and the predicate did not move"
    )


# Ordered: the FIRST refusing guard wins. Budget leads because it is the one
# invariant that is cheap to check and whose violation is unrecoverable.
_GUARDS: tuple[tuple[str, Callable[[dict[str, Any], dict[str, Any]], str | None]], ...] = (
    ("budget", _budget_guard),
    ("destructive", _destructive_guard),
    ("stale-evidence", _stale_evidence_guard),
)


def shield(action: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Evaluate every guard against a proposed action.

    Returns ``{"allow": bool, "guard": str | None, "reason": str}``. A guard
    that raises is treated as refusing — an un-evaluable invariant is not an
    invariant that holds — and the exception is named in the reason so a broken
    guard is visible rather than silently permissive.
    """
    action = action or {}
    context = context or {}
    for name, guard in _GUARDS:
        try:
            reason = guard(action, context)
        except Exception as exc:  # noqa: BLE001 — a broken guard must not vanish
            return {
                "allow": False,
                "guard": name,
                "reason": f"guard {name!r} raised {exc!r}",
            }
        if reason:
            return {"allow": False, "guard": name, "reason": reason}
    return {"allow": True, "guard": None, "reason": ""}
