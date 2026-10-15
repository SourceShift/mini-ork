"""One classifier for a node's terminal state, shared by every reader.

Two surfaces read the same ``node_end`` payload and used to classify it
separately, so they could disagree about one node:

* ``mini_ork.acp.fleet._steps`` classified on ``finish_reason`` alone, so an
  *absent* one became ``done``;
* ``mini_ork.acp.task_state._node_end_failure`` also read ``verdict`` and
  ``error``, so the same payload became a failure.

The reaper's synthetic end (``mini_ork/web/control.py``,
``_close_dangling_node_events``) is exactly that payload — ``verdict: "CRASH"``
with no ``finish_reason`` — which is how a run could read "Failed at test
(CRASH)" in the header while the node's own tab read "Status: done".

The leniency that matters is ``task_state``'s and it is deliberate: a code-fix
implementer's ``node_end`` legitimately carries no ``finish_reason``, so a
missing one with no other failure signal must not be read as failure. This
module is that rule, written once.

Pure functions, no async, no module-level state — same shape as
``mini_ork.acp.task_state`` and ``mini_ork.acp.diffs``.
"""
from __future__ import annotations

from typing import Any

# ``finish_reason`` values a completed node carries. Anything else explicit
# ("error", "timeout", "interrupted", …) is a failure. ``levels_unverified`` is
# the publisher's own abstain signal (``publisher.py`` returns
# ``(0, "levels_unverified")``); it is a withheld publish, not a crashed node —
# treating it as a failure would blame the publisher and make every withheld
# surface decline.
TERMINAL_OK_FINISH = ("done", "skipped", "abstain", "levels_unverified")

# Payload ``verdict`` values that say the node's own judgement was negative.
FAIL_VERDICTS = (
    "request_changes", "escalate", "crash", "needs_revision", "fail", "failed",
)

# String forms a kill-close payload may use instead of a real boolean.
_FALSY = frozenset({"0", "false", "no", "off", ""})

DONE, FAILED = "done", "failed"


def _truthy(value: Any) -> bool:
    """True for a real boolean ``True`` or any string that is not a falsy word."""
    if isinstance(value, str):
        return value.strip().lower() not in _FALSY
    return bool(value)


def node_end_state(payload: dict[str, Any] | None) -> tuple[str, str]:
    """``(state, reason)`` for one ``node_end`` payload.

    ``state`` is ``"failed"`` or ``"done"``; ``reason`` is a short machine
    token (the ``finish_reason``, the ``verdict``, ``"error"``, or
    ``"interrupted"``) and ``""`` on success. Only an explicit failure signal
    counts:

    * a ``finish_reason`` other than :data:`TERMINAL_OK_FINISH`;
    * a ``verdict`` in :data:`FAIL_VERDICTS` (case-insensitive);
    * a non-empty ``error``;
    * ``interrupted: true`` — the reaper's kill marker.

    A missing ``finish_reason`` with no other failure signal is NOT a failure:
    code-fix implementer ``node_end`` events carry none, and the old
    "missing ⇒ unknown ⇒ failed" reading blamed the implementer for a run that
    never failed. Callers that need the third, live state derive it themselves
    (``fleet._steps`` keeps ``running`` while no end event exists).
    """
    payload = payload or {}
    reason = payload.get("finish_reason")
    if reason is not None and str(reason) != "":
        reason = str(reason)
        return (DONE, "") if reason in TERMINAL_OK_FINISH else (FAILED, reason)
    verdict = str(payload.get("verdict") or "")
    if verdict.lower() in FAIL_VERDICTS:
        return (FAILED, verdict)
    if str(payload.get("error") or ""):
        return (FAILED, "error")
    if _truthy(payload.get("interrupted")):
        return (FAILED, "interrupted")
    return (DONE, "")
