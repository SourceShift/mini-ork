"""The ``not_needed`` gesture: a run the operator closed, not the engine.

``task_runs.status`` can already hold ``not_needed`` (migration
``0076_task_runs_status_not_needed.sql``), but a status cannot say *when* the
run was closed, *who* closed it, or — the part ``--undo`` needs — *what it was
before*. So the gesture writes a marker beside the status,
``<run_dir>/not-needed.json``, in the same family as ``landed.json``,
``repair.json``, ``dismissed.json`` and ``retry-gate.json``: a per-run file that
travels with the run directory instead of an index a concurrent writer could
lose.

Why this is not ``dismissed.json``: dismissal means "hide this run from my
inbox" and is deliberately orthogonal to the run's status — a dismissed run is
still executing, and ``ide_pages.inbox`` keeps listing it on the runs page. Here
the run is *over*. One marker cannot mean both, so this is a second one.

Every read is total. A missing, unreadable or malformed marker degrades to
``None``, and the database status alone still reads as terminal — a board render
must never fail because a JSON file lost a brace.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

#: The marker's filename inside a run directory.
MARKER_NAME = "not-needed.json"

#: Detail line for a closed run whose marker is missing or unreadable. The
#: wording is user-facing, so it is pinned here rather than assembled ad hoc.
DEFAULT_DETAIL = "Not needed — closed without running"


def marker_path(run_dir: Path | str) -> Path:
    """The marker for ``run_dir``. A pure path join: nothing is checked."""
    return Path(run_dir) / MARKER_NAME


def read(run_dir: Path | str | None) -> dict[str, Any] | None:
    """The marker for ``run_dir``, or ``None`` when there is not a readable one.

    Returns the parsed dict only when it is a JSON object; a marker holding a
    list or a bare string is treated as absent, because every caller indexes it
    by key and ``None`` is the answer they already handle.
    """
    if run_dir is None:
        return None
    try:
        raw = marker_path(run_dir).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def write(
    run_dir: Path | str,
    previous_status: str | None,
    *,
    reason: str = "",
    by: str = "board",
) -> dict[str, Any]:
    """Write the marker and return it. Propagates ``OSError`` to the caller.

    ``previous_status`` is recorded even when it is ``None`` (a run with no row,
    or a NULL status) — ``--undo`` must be able to restore "whatever it was",
    and an absent key would be indistinguishable from a lost one.
    """
    marker: dict[str, Any] = {
        "status": "not_needed",
        "previous_status": previous_status,
        "at": int(time.time()),
        "by": by,
    }
    if reason:
        marker["reason"] = reason
    path = marker_path(run_dir)
    path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return marker


def clear(run_dir: Path | str) -> bool:
    """Remove the marker. ``True`` when one was there, ``False`` otherwise.

    Mirrors ``Path.unlink(missing_ok=True)``'s tolerance, but reports which case
    it was so ``--undo`` on an already-restored run is not a silent no-op.
    """
    path = marker_path(run_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
