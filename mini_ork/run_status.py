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
