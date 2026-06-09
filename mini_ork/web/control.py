"""Control-plane primitives for mini-ork task_runs: stop, kill, status writeback.

Why a separate module: the rest of the obs surface is strictly read-only.
This module is the ONLY place that performs side effects (touch a file,
send a signal, write to state.db). Keeping it isolated makes the
read-only invariant auditable.

Security model: bound to 127.0.0.1 by default (see bin/mini-ork-serve).
For local dev that's sufficient. If you ever expose the API beyond
loopback, add CSRF + auth before allowing these endpoints to fire —
browsers will let malicious pages issue POSTs to localhost otherwise.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any

from .db import StateDB

# Statuses that may be controlled. Terminal statuses are excluded — killing
# a published run is a no-op pattern that almost certainly means the user
# is looking at the wrong row.
CONTROLLABLE_STATUSES = {
    "classified",
    "planned",
    "executing",
    "verifying",
    "reviewing",
}


def _resolve_run_dir(home: Path, task_run_id: str) -> Path:
    return home / "runs" / task_run_id


def stop_run(home: Path, db: StateDB, task_run_id: str) -> dict[str, Any]:
    """Soft stop: touch .stop-requested in the run dir.

    The dispatcher (bin/mini-ork-execute:_dispatch_node) checks this flag
    before each node dispatch and bails cleanly. The current in-flight
    node finishes naturally — that's the soft-stop semantic.
    """
    tr = db.row("SELECT id, status FROM task_runs WHERE id = ?", (task_run_id,))
    if not tr:
        return {"ok": False, "error": "task_run not found", "task_run_id": task_run_id}
    if tr["status"] not in CONTROLLABLE_STATUSES:
        return {
            "ok": False,
            "error": f"task_run status '{tr['status']}' not controllable (already terminal)",
            "task_run_id": task_run_id,
        }

    run_dir = _resolve_run_dir(home, task_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    flag = run_dir / ".stop-requested"
    flag.write_text(f"{int(time.time())}\n", encoding="utf-8")

    return {
        "ok": True,
        "task_run_id": task_run_id,
        "action": "stop",
        "flag_path": str(flag),
        "note": "dispatcher will exit cleanly before the next node; current node finishes",
    }


def kill_run(home: Path, db: StateDB, task_run_id: str) -> dict[str, Any]:
    """Hard kill: SIGTERM → 2s grace → SIGKILL the dispatcher pid.

    Process tree termination is best-effort: bash's nested subshells may
    leak children that we don't see in .pid. Falls back to pgrep matching
    the run_id when .pid isn't present (e.g. crash before write).
    """
    tr = db.row("SELECT id, status FROM task_runs WHERE id = ?", (task_run_id,))
    if not tr:
        return {"ok": False, "error": "task_run not found", "task_run_id": task_run_id}

    run_dir = _resolve_run_dir(home, task_run_id)
    pid_file = run_dir / ".pid"
    pids: list[int] = []
    if pid_file.exists():
        try:
            pids = [int(p) for p in pid_file.read_text().split() if p.strip().isdigit()]
        except (OSError, ValueError):
            pids = []

    # Fallback: pgrep for any bash/python process whose cmdline contains the
    # task_run_id. Useful when the .pid file is missing or stale.
    if not pids:
        try:
            import subprocess

            out = subprocess.run(
                ["pgrep", "-f", task_run_id],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if out.returncode == 0:
                pids = [int(p) for p in out.stdout.split() if p.strip().isdigit()]
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pids = []

    killed: list[int] = []
    survived: list[int] = []

    # Phase 1: SIGTERM
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            continue
        except PermissionError:
            survived.append(pid)

    if killed:
        time.sleep(2.0)
        # Phase 2: SIGKILL anything still alive
        for pid in killed:
            try:
                os.kill(pid, 0)  # probe — raises if dead
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except PermissionError:
                survived.append(pid)

    # Writeback to task_runs — mark failed regardless of whether pids were
    # found. The user clicked Kill; the row should reflect that intent.
    _writeback_terminal(db, task_run_id, status="failed", notes="killed-by-user")

    # Clean up .pid + .stop-requested artifacts so the row doesn't look
    # like a still-active run.
    for f in (run_dir / ".pid", run_dir / ".stop-requested"):
        try:
            f.unlink()
        except FileNotFoundError:
            pass

    # Fix C: when the killed run is part of a recursive-self-improve session,
    # touch the session kill-flag so the OUTER loop halts cleanly after the
    # current iter. Killing one iter implicitly means "don't auto-start the
    # next one" — otherwise the user would have to play whack-a-mole.
    session_halted = False
    if task_run_id.startswith("self-improve-iter-"):
        kill_flag = home / "state" / ".self-improve-kill"
        kill_flag.parent.mkdir(parents=True, exist_ok=True)
        kill_flag.write_text(f"{int(time.time())}\n", encoding="utf-8")
        session_halted = True

    return {
        "ok": True,
        "task_run_id": task_run_id,
        "action": "kill",
        "pids_targeted": pids,
        "pids_signaled": killed,
        "pids_survived_permission_denied": survived,
        "session_halted": session_halted,
        "note": (
            "no pids found — marked status=failed anyway; dispatcher may have already exited"
            if not pids
            else "SIGTERM then SIGKILL escalation; task_runs.status set to 'failed'"
        )
        + (
            "; self-improve session kill-flag touched — outer loop will halt after current iter"
            if session_halted
            else ""
        ),
    }


def _writeback_terminal(db: StateDB, task_run_id: str, status: str, notes: str) -> None:
    """Write a terminal status + notes to task_runs. Uses raw sqlite write
    (db is read-only). We open our own connection here — the read-only
    connection from get_db() will refuse writes."""
    import sqlite3

    db_path = str(db.db_path)
    now = int(time.time())
    con = sqlite3.connect(db_path, timeout=5.0)
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        con.execute(
            """
            UPDATE task_runs
               SET status = ?,
                   notes = COALESCE(notes || '; ', '') || ?,
                   updated_at = ?,
                   ended_at = COALESCE(ended_at, ?)
             WHERE id = ?
            """,
            (status, notes, now, now, task_run_id),
        )
        con.commit()
    finally:
        con.close()
