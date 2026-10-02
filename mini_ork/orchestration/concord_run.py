"""``mini-ork`` run principals — every run registers itself with Concord.

P1a: a ``mini-ork run`` becomes a Concord principal automatically, so the node
workers it spawns (claude / opencode CLI sessions) inherit ``CONCORD_PRINCIPAL``
and are attributed to the run instead of appearing as unrelated sessions.

Unlike ``mini-ork concord run`` (``orchestration/concord.py``), the run
principal is NOT a process-group wrapper: it never starts a session, never sets
a pgid, and never kills anything. It only registers the run, heartbeats it, and
ends it on teardown. Every coord call fails open with the existing short
timeout — Concord must never fail or delay a run, so a ContextNest outage
prints one stderr line and the run proceeds without a heartbeat.

The wire contract is pinned in ``docs/architecture/concord-protocol.md``; the
raise-based helpers live in ``mini_ork.cn_client``.
"""
from __future__ import annotations

import os
import re
import socket
import sys
import threading
from dataclasses import dataclass

from mini_ork import cn_client

__all__ = ["start", "stop", "RunPrincipal"]

# The name half of a principal id: keep ``[A-Za-z0-9._@/-]``, replace anything
# else with ``_``. Substituting (rather than dropping) preserves id length and
# keeps ``a!b`` distinct from ``ab``.
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._@/-]")


def _sanitize_run_id(run_id: str) -> str:
    """Map ``run_id`` to a valid principal name (``[A-Za-z0-9._@/-]{1,128}``)."""
    return _SANITIZE_RE.sub("_", run_id)[:128] or "run"


@dataclass
class RunPrincipal:
    """A registered run principal and its live heartbeat.

    ``thread`` is None when registration failed (fail-open: the env var stays
    set but no heartbeat is started), which is how ``stop`` keeps the heartbeat
    half a no-op while still restoring the env var.
    """

    principal_id: str
    parent: str | None
    fields: dict
    stop_event: threading.Event
    thread: threading.Thread | None = None


def _heartbeat_interval() -> int:
    try:
        return max(1, int(os.environ.get("MO_CONCORD_HEARTBEAT_S", "30")))
    except (TypeError, ValueError):
        return 30


def _heartbeat(handle: RunPrincipal) -> None:
    interval = _heartbeat_interval()
    while not handle.stop_event.wait(interval):
        try:
            cn_client.coord_upsert_principal(handle.principal_id, handle.fields)
        except (cn_client.CoordUnavailable, cn_client.CoordHTTPError):
            pass  # fail open: retry on the next interval, silently


def start(run_id: str, recipe: str, kickoff: str) -> RunPrincipal | None:
    """Register the run as a Concord principal and start its heartbeat.

    Returns None when ``MO_CONCORD=0`` (before touching the env). On a
    ContextNest failure it fails open: one stderr line, the env var stays set,
    and no heartbeat is started.
    """
    if os.environ.get("MO_CONCORD", "1") == "0":
        return None
    principal_id = f"run:{_sanitize_run_id(run_id)}"
    parent = os.environ.get("CONCORD_PRINCIPAL")
    fields: dict = {
        "harness": "mini-ork",
        "host": socket.gethostname(),
        "cwd": os.getcwd(),
        "worktree": os.environ.get("MO_TARGET_CWD") or None,
        "pids": [os.getpid()],
        "kill_recipe": [f"kill -TERM {os.getpid()}"],
        "labels": {"recipe": recipe, "kickoff": os.path.basename(kickoff)},
    }
    if parent:
        fields["labels"]["parent"] = parent
    os.environ["CONCORD_PRINCIPAL"] = principal_id
    handle = RunPrincipal(principal_id, parent, fields, threading.Event())
    try:
        cn_client.coord_upsert_principal(principal_id, fields)
    except (cn_client.CoordUnavailable, cn_client.CoordHTTPError):
        sys.stderr.write("[concord] ContextNest unavailable — run not registered\n")
        return handle  # env stays set; no heartbeat
    handle.thread = threading.Thread(target=_heartbeat, args=(handle,), daemon=True)
    handle.thread.start()
    return handle


def stop(handle: RunPrincipal | None) -> None:
    """Stop the heartbeat, end the principal, and restore ``CONCORD_PRINCIPAL``.

    Idempotent; ``stop(None)`` is a no-op. Every coord call fails open.
    """
    if handle is None:
        return
    handle.stop_event.set()
    if handle.thread is not None:
        handle.thread.join(timeout=0.5)  # daemon thread: never delay run teardown
    try:
        cn_client.coord_end_principal(handle.principal_id)
    except (cn_client.CoordUnavailable, cn_client.CoordHTTPError):
        pass  # fail open on teardown too
    if handle.parent is None:
        os.environ.pop("CONCORD_PRINCIPAL", None)
    else:
        os.environ["CONCORD_PRINCIPAL"] = handle.parent
