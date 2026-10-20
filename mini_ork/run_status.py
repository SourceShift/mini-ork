"""The one definition of what a ``task_runs.status`` value means.

``task_runs.status`` is CHECK-constrained in the schema
(``db/migrations/0013_task_runs.sql``, widened by
``0076_task_runs_status_not_needed.sql``), so the storage layer already refuses
a value nobody defined. What it cannot express is which of those values mean
the run is over, and which mean it went wrong — and before this module every
reader answered that question for itself: the terminal set was spelled out as a
Python literal in six modules and as an inline SQL fragment in nine more. The
cost is not hypothetical; adding ``not_needed`` had to find all twenty-one.

Two sets, because "over" and "unsuccessful" are different questions:

* :data:`TERMINAL` — the run will never move again. Every reader that decides
  whether to *wait* wants this set: the reaper, the board's state marks, an SSE
  stream, a liveness index. A ``not_needed`` run is terminal — nothing is
  coming, so waiting on it is a bug.
* :data:`UNSUCCESSFUL` — the run did not deliver. Repair, metrics and the
  failure banners want this set, and a ``not_needed`` run must stay OUT of it:
  a run the operator closed as unwanted is not a defect to auto-repair.

:data:`LIVE` is the complement of :data:`TERMINAL` — the statuses a run passes
through while it is still moving. ``TERMINAL | LIVE == STATUSES``, disjoint, is
the invariant ``tests/unit/test_run_status.py`` pins against both this module
and the schema's CHECK list; a status added to the schema without a home here
fails that test rather than silently reading as in-flight on the board.
"""
from __future__ import annotations

import datetime as _dt
import re as _re

#: The statuses a run passes through while it is still moving.
LIVE = frozenset({"classified", "planned", "executing", "verifying", "reviewing"})

#: The statuses that mean the run is over and will never move again.
TERMINAL = frozenset({"published", "failed", "rolled_back", "not_needed"})

#: The terminal statuses that mean the run did not deliver its work.
UNSUCCESSFUL = frozenset({"failed", "rolled_back"})

#: Every value the schema's CHECK constraint admits.
STATUSES = LIVE | TERMINAL

#: The four board buckets plus ``not_needed``. The IDE's sidebar renders the
#: first four today and ignores a key it does not know, so this tuple growing
#: by one is safe for an older client (the same posture as ``Board.total``'s
#: ``#[serde(default)]``).
BUCKETS = ("working", "needs_you", "done", "failed", "not_needed")

#: A terminal status → the board bucket it counts under. A live status has no
#: entry: ``run_mark`` buckets those as ``working``, which is the one place
#: that default belongs.
DEFAULT_BUCKET = {
    "published": "done",
    "failed": "failed",
    "rolled_back": "failed",
    "not_needed": "not_needed",
}

#: Human wording for a status, for the pages that print one. Keyed by status,
#: not by bucket — a page that wants the bucket wants ``DEFAULT_BUCKET``.
LABELS = {
    "classified": "Classified",
    "planned": "Planned",
    "executing": "Executing",
    "verifying": "Verifying",
    "reviewing": "Reviewing",
    "published": "Published",
    "rolled_back": "Rolled back",
    "failed": "Failed",
    "not_needed": "Not needed",
}


def _sql_in(values: frozenset[str]) -> str:
    """A literal ``IN`` list for the readers that filter in SQL, not in Python.

    Sorted so the emitted text is stable — a test can pin it, and a diff of an
    SQL string is readable instead of hash-ordered.
    """
    return "(" + ",".join(f"'{value}'" for value in sorted(values)) + ")"


#: ``WHERE status IN ...`` fragments for the call sites that cannot import a
#: Python set. Two of them, mirroring the two sets above.
SQL_TERMINAL = _sql_in(TERMINAL)
SQL_UNSUCCESSFUL = _sql_in(UNSUCCESSFUL)


def is_terminal(status: str | None) -> bool:
    """True when ``status`` names a run that will never move again."""
    return status in TERMINAL


def label(status: str | None) -> str:
    """A status's human wording; an unknown status prints as itself.

    Falling back to the raw value rather than a placeholder is deliberate: a
    page showing an unrecognised status should show the token the database
    actually holds, which is the thing an operator can grep for.
    """
    return LABELS.get(status or "", status or "")


#: ``attempt <n> started <UTC iso>`` — the marker a live-status write appends
#: (``cli.execute.set_status`` / ``web.control._restore_status``) and the one
#: place a close path can learn when the CURRENT attempt began.
#:
#: The stamp is matched as a timestamp, not ``\S+``: notes are ``"; "``-joined
#: and an attempt is routinely followed by a mid-attempt breadcrumb
#: (``impl_moved_base``, ``VERIFIER_NOT_EXECUTED``, …). ``\S+`` swallowed that
#: separator's ``;``, the parse failed, and the close silently fell back to
#: ``updated_at``/``created_at`` — the idle-gap bug again, through the back door.
_ATTEMPT_MARKER = _re.compile(
    r"attempt\s+(\d+)\s+started\s+"
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))")


def _marker_epoch(stamp: str) -> int | None:
    """Parse a marker stamp (ISO 8601, ``Z`` or numeric offset) to epoch seconds."""
    try:
        parsed = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return int(parsed.timestamp())


def attempt_start_epoch(notes, *, updated_at=None, created_at=None,
                        ended_at=None):
    """Epoch second at which the run's current attempt began.

    The ``attempt <n> started <iso>`` marker in ``notes`` is authoritative when
    present: the highest-numbered marker is the live attempt, and using it is
    what keeps a revive's idle gap out of ``duration_ms``. Rows that predate
    the marker fall back to ``updated_at`` (the last live write, which for a
    pre-marker row is the closest thing to an attempt start) and then
    ``created_at``.

    A heartbeat stamped at or after ``ended_at`` is clamped to ``ended_at``, not
    discarded: the row is read BEFORE the closing UPDATE, so equal timestamps
    simply mean "this attempt began in the same second the run closed" — a
    zero-length segment. Falling back to ``created_at`` there folds the run's
    whole age (and every idle day) back in. Returns ``None`` when no timestamp
    is available at all.
    """
    best_number, best_stamp = 0, None
    for match in _ATTEMPT_MARKER.finditer(str(notes or "")):
        stamp = _marker_epoch(match.group(2))
        if stamp is None:
            continue
        number = int(match.group(1))
        if number >= best_number:
            best_number, best_stamp = number, stamp
    if best_stamp is not None:
        return best_stamp
    if updated_at is not None:
        updated_at = int(updated_at)
        if ended_at is not None:
            updated_at = min(updated_at, int(ended_at))
        return updated_at
    if created_at is not None:
        return int(created_at)
    return None


def close_duration_ms(accumulated=0, notes=None, *, ended_at, updated_at=None,
                      created_at=None, already_closed=False):
    """The one terminal-close ``duration_ms``: accumulated + this attempt.

    ``duration_ms`` is the run's ACTIVE time, not its age. A revive keeps the
    time already accumulated (the live-status write no longer zeroes it), so a
    terminal close adds ONLY the current attempt — ``ended_at`` minus the
    attempt start from :func:`attempt_start_epoch` — instead of recomputing
    ``ended_at - created_at`` and folding the idle gap in.

    The result is capped at the row's wall age (``ended_at - created_at``): no
    active-time sum can legitimately exceed it, so a legacy row whose
    ``duration_ms`` was inflated by the old ``created_at`` formula must not be
    handed a longer duration than it has existed.

    ``already_closed`` guards a second close of the same attempt (the publisher,
    the executor's finalizer and the reaper can all write one end): adding the
    segment twice would inflate a run's active time.
    """
    accumulated = max(int(accumulated or 0), 0)
    ended_at = int(ended_at)
    if already_closed and accumulated:
        return accumulated
    start = attempt_start_epoch(
        notes, updated_at=updated_at, created_at=created_at,
        ended_at=ended_at)
    if start is None:
        return accumulated
    duration = accumulated + max(ended_at - int(start), 0) * 1000
    if created_at is not None:
        duration = min(duration, max((ended_at - int(created_at)) * 1000, 0))
    return max(duration, 0)
