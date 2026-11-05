"""The thread that orchestrates a run.

A run is watched by a conversation, not by a page: the thread narrates the
run's stage boundaries and carries an operator's instruction back into it. The
binding is written when a thread adopts a run (``/orchestrator <run>``, or the
orchestrator's own ``start_run``) and read by the IDE pages, which need it to
open the thread that already watches the run rather than mint an empty one.

The record sits beside ``owner.json`` in the run directory because it answers a
different question: ``owner.json`` says who is notified about the run, this says
which conversation is watching it. A run may be owned by the user and still be
orchestrated by a thread, so the two are not merged.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

FILENAME = "orchestrator.json"
OWNER_FILENAME = "owner.json"


def run_dir(home: str | Path, run_id: str) -> Path:
    """Where ``run_id``'s artifacts live under ``home``.

    A user-launched run sits at ``<home>/runs/<id>``. An rsi cycle gives each
    child run its own home, one directory deeper — ``<home>/rsi/<candidate>/
    <step>/home/runs/<id>``, or ``…/child-home/runs/<id>`` when the cycle
    stages the exercise base that way — while the row the IDE lists still
    comes from the parent ``state.db``. Both shapes resolve here so a page
    and the agent that binds it agree on the one directory they read.

    A missing directory returns the direct path unchanged: callers already
    treat "no run dir" as "nothing to read", and a run id that could escape
    the tree never reaches the glob.
    """
    home = Path(home)
    direct = home / "runs" / run_id
    if direct.is_dir() or not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        return direct
    for pattern in (f"rsi/*/*/home/runs/{run_id}", f"rsi/*/child-home/runs/{run_id}"):
        for candidate in sorted(home.glob(pattern)):
            if candidate.is_dir():
                return candidate
    return direct


def read_run(home: str | Path, run_id: str) -> str | None:
    """The orchestrator bound to ``run_id`` under ``home`` (any run layout)."""
    return read(run_dir(home, run_id))


def read(run_dir: str | Path) -> str | None:
    """The session id of the thread watching ``run_dir``, or ``None``.

    ``owner.json`` is the fallback: a run launched from the launcher carries
    ``MO_RUN_OWNER=thread:<session>`` and may have been bound before this file
    existed. Reading it keeps those runs openable in the thread that started
    them.
    """
    run_dir = Path(run_dir)
    bound = _load(run_dir / FILENAME)
    if isinstance(bound, dict):
        session_id = str(bound.get("session_id") or "").strip()
        if session_id:
            return session_id
    owner = _load(run_dir / OWNER_FILENAME)
    if isinstance(owner, dict) and str(owner.get("kind") or "") == "thread":
        return str(owner.get("id") or "").strip() or None
    return None


def write(run_dir: str | Path, session_id: str) -> bool:
    """Record ``session_id`` as the run's orchestrator; ``False`` if it cannot.

    Only an existing run directory is written. A follow is also started for run
    ids that have no directory here (test harnesses, a pruned run), and those
    must not grow one — the file describes a run, it never creates it.
    """
    session_id = str(session_id or "").strip()
    run_dir = Path(run_dir)
    if not session_id or not run_dir.is_dir():
        return False
    payload = {"session_id": session_id, "bound_at": int(time.time())}
    try:
        (run_dir / FILENAME).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError:
        return False
    return True


def _load(path: Path) -> Any:
    """Parse a JSON file, or ``None`` when it is absent or unreadable."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None
