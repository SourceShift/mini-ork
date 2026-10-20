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

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_ork import not_needed as _not_needed
from mini_ork import run_status as _run_status
from mini_ork.features import gate_env

from .db import StateDB

# Statuses that may be controlled. Terminal statuses are excluded — killing
# a published run is a no-op pattern that almost certainly means the user
# is looking at the wrong row. ``run_status.LIVE`` is that complement.
CONTROLLABLE_STATUSES = frozenset(_run_status.LIVE)

#: The schema value a closed run's ``task_runs.status`` takes.
NOT_NEEDED = "not_needed"

#: The breadcrumb the closure appends to ``task_runs.notes``. Shared with
#: :func:`_strip_trailing_note` so the writer and the remover cannot drift: an
#: undo that stripped a different spelling would leave the note behind and the
#: next close would append a second one.
NOT_NEEDED_NOTE = "not-needed"


def _resolve_run_dir(home: Path, task_run_id: str) -> Path:
    return home / "runs" / task_run_id


def pause_cost_run(
    home: Path, task_run_id: str, threshold_usd: float = 25.0
) -> dict[str, Any]:
    """Write the cost-pause sentinel manually.

    Companion to lib/cost_pause.sh (Epic E4). The HTTP layer lets
    operators trigger a pause without waiting for the cost-threshold
    crossing. The dispatcher (mini_ork/cli/execute.py) checks the
    sentinel before each LLM call and bails with
    finish_reason=paused_for_approval. Resume via the /resume-cost
    POST or via `bin/mini-ork-resume <run_id>`.
    """
    run_dir = _resolve_run_dir(home, task_run_id)
    if not run_dir.is_dir():
        return {"ok": False, "error": "run_dir not found", "task_run_id": task_run_id}

    sentinel = run_dir / ".cost-pause"
    payload = {
        "threshold_usd": threshold_usd,
        "spent_usd": 0.0,  # operator-triggered; cost counter untouched
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": task_run_id,
        "source": "http-api",
    }
    sentinel.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    return {
        "ok": True,
        "task_run_id": task_run_id,
        "action": "pause-cost",
        "sentinel_path": str(sentinel),
        "threshold_usd": threshold_usd,
        "note": (
            "dispatcher will hold further LLM calls; resume via "
            "POST /api/v1/task-runs/{id}/resume-cost or "
            "bin/mini-ork-resume"
        ),
    }


def resume_cost_run(
    home: Path, task_run_id: str, approver: str
) -> dict[str, Any]:
    """Clear the cost-pause sentinel + record the approval.

    Mirrors bin/mini-ork-resume's audit shape: appends
    {resumed_at, approver, source, run_id} to
    .cost-pause-approvals.jsonl so the resume is traceable. Returns
    ok=False when no sentinel was present (treat as a successful
    no-op on the client side).
    """
    run_dir = _resolve_run_dir(home, task_run_id)
    if not run_dir.is_dir():
        return {"ok": False, "error": "run_dir not found", "task_run_id": task_run_id}

    sentinel = run_dir / ".cost-pause"
    if not sentinel.is_file():
        return {
            "ok": False,
            "error": "no cost-pause sentinel present",
            "task_run_id": task_run_id,
        }

    sentinel_payload = sentinel.read_text(encoding="utf-8").strip()
    approvals = run_dir / ".cost-pause-approvals.jsonl"
    audit = {
        "resumed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "approver": approver,
        "source": "http-api",
        "run_id": task_run_id,
        "sentinel_payload": sentinel_payload,
    }
    with approvals.open("a", encoding="utf-8") as f:
        f.write(json.dumps(audit) + "\n")

    sentinel.unlink()

    return {
        "ok": True,
        "task_run_id": task_run_id,
        "action": "resume-cost",
        "approver": approver,
        "audit_path": str(approvals),
    }


def stop_run(home: Path, db: StateDB, task_run_id: str) -> dict[str, Any]:
    """Soft stop: touch .stop-requested in the run dir.

    The dispatcher (mini_ork/cli/execute.py:_dispatch_node) checks this flag
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


def _with_descendants(pids: list[int]) -> list[int]:
    """``pids`` plus every live descendant, parents first.

    Must be snapshotted BEFORE any signal: a killed parent's children are
    re-parented to init at once and can no longer be found from it. Never
    includes this process or init. Best-effort: a failing ``ps`` returns
    ``pids`` unchanged.
    """
    if not pids:
        return pids
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return pids
    if out.returncode != 0:
        return pids
    children: dict[int, list[int]] = {}
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        children.setdefault(int(parts[1]), []).append(int(parts[0]))
    tree: list[int] = []
    queue = list(pids)
    while queue:
        pid = queue.pop(0)
        if pid <= 1 or pid == os.getpid() or pid in tree:
            continue
        tree.append(pid)
        queue.extend(children.get(pid, []))
    return tree


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
        # The run's pool workers and a recipe's dispatcher subprocess carry no
        # run id in their argv, so only the process tree reaches them. Killing
        # just the owner left run-1791750091-88029's per-spec dispatcher
        # running for two hours on 2026-10-13, launching new children into a
        # killed run. Only the .pid owner is expanded: a pgrep match below can
        # be a caller's own shell, whose tree must not be swept.
        pids = _with_descendants(pids)

    # Fallback: pgrep for any bash/python process whose cmdline contains the
    # task_run_id. Useful when the .pid file is missing or stale.
    if not pids:
        try:
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

    # Remote-nodes-02 (D2) / cloud-providers A2: marker-driven reap. SIGKILL
    # bypasses the executor's Python ``finally`` (which would call
    # ``Workspace.down()``), so the run-scoped container or node session
    # survives until the 6 h TTL reaper runs. One call into the teardown
    # registry reaps it now — the registry resolves the backend's hook lazily
    # (docker ``rm -f``, the remote session's ``kill_all`` + ``down``, or any
    # backend that registered one), so this ``serve`` process needs neither the
    # executor's env nor an import of the backend. Best-effort: the reaper
    # never raises, and a marker-less run is a no-op.
    _marker_reaped = False
    try:
        from mini_ork.runtime.workspace_session import session_marker_paths

        has_marker = bool(session_marker_paths(run_dir))
    except Exception:  # noqa: BLE001 — no readable run dir: nothing to reap here
        has_marker = False
    if has_marker:
        try:
            from mini_ork.runtime.sandbox_reaper import reap_by_marker

            reap_by_marker(run_dir)
            _marker_reaped = True
        except Exception:  # noqa: BLE001 — fallback: the TTL reaper sweeps next tick
            pass

    # Writeback to task_runs — mark failed regardless of whether pids were
    # found. The user clicked Kill; the row should reflect that intent.
    _writeback_terminal(db, task_run_id, status="failed", notes="killed-by-user", verdict="CRASH")

    # SIGKILL bypasses the dispatcher's EXIT-trap finalizer, so dangling
    # node_start events (UI DAG "running" forever) must be closed here too.
    _close_dangling_node_events(db, task_run_id)

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
        "session_marker_reaped": _marker_reaped,
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


def get_profile(home: Path, task_run_id: str) -> dict[str, Any]:
    """Read run_profile.json for a task_run — returns the planner's human_questions,
    confidence score, and any existing answers (from profile-answers.json).

    The UI uses this to drive the interactive Q&A panel: when a kickoff is
    vague, the planner emits clarifying questions; the user answers; mini-ork
    re-runs with answers injected into the kickoff context.
    """
    run_dir = _resolve_run_dir(home, task_run_id)
    profile_path = run_dir / "run_profile.json"
    answers_path = run_dir / "profile-answers.json"

    profile: dict[str, Any] = {}
    if profile_path.exists():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            profile = {}

    answers: dict[str, str] = {}
    if answers_path.exists():
        try:
            answers = json.loads(answers_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            answers = {}

    questions = profile.get("human_questions") or []
    status = profile.get("profile_status", "")
    return {
        "task_run_id": task_run_id,
        "profile_path": str(profile_path) if profile_path.exists() else None,
        "answers_path": str(answers_path) if answers_path.exists() else None,
        "profile_status": status,
        "confidence": profile.get("confidence", 0.0),
        "questions": questions,
        "answers": answers,
        "needs_answers": status == "needs_answers" and len(questions) > len(answers),
    }


def save_answers(
    home: Path,
    task_run_id: str,
    answers: dict[str, str],
) -> dict[str, Any]:
    """Persist user answers + augment run_profile.json so a re-dispatch picks them up.

    Writes two files:
      - <run_dir>/profile-answers.json (the user-supplied map)
      - <run_dir>/run_profile.json (merged: answers folded into profile,
        profile_status flipped to 'ready' so the gate stops blocking)

    Returns a dict with the recommended next-step CLI to continue the run.
    """
    run_dir = _resolve_run_dir(home, task_run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    # Persist raw answers
    answers_path = run_dir / "profile-answers.json"
    answers_path.write_text(json.dumps(answers, indent=2), encoding="utf-8")

    # Merge into run_profile.json so the next planner invocation sees them
    profile_path = run_dir / "run_profile.json"
    profile: dict[str, Any] = {}
    if profile_path.exists():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            profile = {}

    profile.setdefault("answers", {}).update(answers)
    # Mark ready so MINI_ORK_PROFILE_GATE no longer blocks
    profile["profile_status"] = "ready"
    profile["confidence"] = max(float(profile.get("confidence", 0.0)), 0.9)
    profile["human_questions"] = []  # Cleared — user has answered them
    profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")

    # Find the kickoff so the suggested re-run command is exact
    kickoff = run_dir / "kickoff.md"
    suggest_cli = (
        f"mini-ork run {profile.get('recipe', '<recipe>')} {kickoff}"
        if kickoff.exists()
        else f"mini-ork run <recipe> <kickoff.md>  # answers saved at {answers_path}"
    )

    return {
        "ok": True,
        "task_run_id": task_run_id,
        "answers_saved": list(answers.keys()),
        "answers_path": str(answers_path),
        "profile_path": str(profile_path),
        "profile_status": "ready",
        "next_step_cli": suggest_cli,
        "note": (
            "answers persisted + run_profile.json updated; re-run the CLI to continue. "
            "Future versions will auto-resume."
        ),
    }


def _writeback_terminal(
    db: StateDB, task_run_id: str, status: str, notes: str, verdict: str | None = None
) -> None:
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
                   verdict = COALESCE(verdict, ?),
                   notes = COALESCE(notes || '; ', '') || ?,
                   updated_at = ?,
                   ended_at = COALESCE(ended_at, ?)
             WHERE id = ?
            """,
            (status, verdict, notes, now, now, task_run_id),
        )
        con.commit()
    finally:
        con.close()


def _strip_trailing_note(notes: str | None, tag: str) -> str | None:
    """Drop trailing ``; <tag>[: …]`` segments a since-reversed gesture wrote.

    Only the tag's own segments are removed, and only from the end — a run's
    earlier notes (its ``impl_moved_base`` reason, say) must survive verbatim.
    Looped so an already-accumulated pair cleans up in one undo.
    """
    if notes is None:
        return None
    pattern = re.compile(rf"(?:^|;\s*){re.escape(tag)}(?::.*)?\s*$")
    text = notes
    while True:
        stripped = pattern.sub("", text)
        if stripped == text:
            break
        text = stripped
    return text.strip() or None


_ATTEMPT_MARKER_RE = re.compile(r"^attempt (\d+) started\b")


def _attempt_boundary_note(notes: str | None, *, prior_attempt: bool) -> str:
    """The ``attempt <n> started <UTC iso>`` segment a live-status write appends.

    Mirrors ``cli.execute.set_status``: ``n`` continues the marker already in
    the append-only notes; a row whose attempt 1 predates the marker (it still
    carries a verdict or an end stamp) is revived as attempt 2.
    """
    numbers = [int(match.group(1)) for segment in (notes or "").split("; ")
               if (match := _ATTEMPT_MARKER_RE.match(segment.strip()))]
    last = max(numbers, default=0)
    n = last + 1 if last else (2 if prior_attempt else 1)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"attempt {n} started {stamp}"


def _restore_status(
    db_path: str, task_run_id: str, status: str, *, drop_note: str | None = None
) -> None:
    """Put a run back to ``status``, the undo half of :func:`mark_not_needed`.

    ``ended_at`` is cleared only when the restored status is live. A run
    reopened mid-flight must not keep the end stamp the closure gave it, while
    one restored to ``failed`` or ``published`` must keep the stamp it already
    had — clearing it there would make a settled run read as still running.
    The live write is also the attempt boundary: it clears the dead attempt's
    verdict and appends ``attempt <n> started`` so a revived run is not
    diagnosed from the previous attempt's notes (RB-16).

    ``drop_note`` names a breadcrumb the reversed gesture appended; it is
    removed in the same write so a close→undo→close cycle leaves exactly one.
    """
    import sqlite3

    live = not _run_status.is_terminal(status)
    now = int(time.time())
    con = sqlite3.connect(db_path, timeout=5.0)
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        if not live and drop_note is None:
            con.execute(
                "UPDATE task_runs SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, task_run_id),
            )
        else:
            row = con.execute(
                "SELECT notes, verdict, ended_at FROM task_runs WHERE id = ?",
                (task_run_id,),
            ).fetchone()
            notes = row[0] if row else None
            if drop_note is not None:
                notes = _strip_trailing_note(notes, drop_note)
            assignments = ["status = ?", "updated_at = ?"]
            values: list[Any] = [status, now]
            if live:
                prior_attempt = bool(
                    row and (row[1] is not None or row[2] is not None))
                marker = _attempt_boundary_note(
                    notes, prior_attempt=prior_attempt)
                merged = f"{notes}; {marker}" if notes else marker
                assignments += ["verdict = NULL", "ended_at = NULL",
                                "duration_ms = 0", "notes = ?"]
                values.append(merged)
            elif drop_note is not None:
                assignments.append("notes = ?")
                values.append(notes)
            values.append(task_run_id)
            con.execute(
                f"UPDATE task_runs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
        con.commit()
    finally:
        con.close()


def mark_not_needed(
    home: Path,
    db: StateDB,
    task_run_id: str,
    *,
    undo: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    """Close a run as unwanted work, or reopen it (``--undo``).

    This is not :func:`_act_dismiss`'s dismissal, and the two must not be
    merged: dismissal *hides* a row and leaves the run wherever it was — a
    dismissed run may still be executing. Closing means the run is over.

    ``task_runs.status`` alone cannot carry this. ``--undo`` has to restore what
    the run *was*, and a status column holding ``not_needed`` does not remember,
    so the closure writes ``<run_dir>/not-needed.json`` with the previous value.
    The marker goes down FIRST: if the status write then fails we hold a marker
    with no closure, which ``--undo`` still handles, whereas the reverse would
    strand a closed run that nothing can reopen.
    """
    tr = db.row("SELECT id, status FROM task_runs WHERE id = ?", (task_run_id,))
    if not tr:
        return {"ok": False, "task_run_id": task_run_id, "error": "task_run not found"}

    run_dir = _resolve_run_dir(home, task_run_id)
    current = str(tr["status"] or "")

    if undo:
        marker = _not_needed.read(run_dir)
        if marker is None:
            return {
                "ok": False,
                "task_run_id": task_run_id,
                "error": "this run was not closed as not-needed — nothing to restore",
            }
        previous = marker.get("previous_status")
        if not isinstance(previous, str) or not previous:
            # A marker with no usable previous value cannot be undone
            # honestly; refusing beats inventing a status to restore.
            return {
                "ok": False,
                "task_run_id": task_run_id,
                "error": "the not-needed marker records no previous status",
            }
        _restore_status(
            str(db.db_path), task_run_id, previous, drop_note=NOT_NEEDED_NOTE
        )
        _not_needed.clear(run_dir)
        return {
            "ok": True, "task_run_id": task_run_id, "action": "not_needed",
            "mode": "restored", "status": previous,
        }

    if current == NOT_NEEDED:
        # Idempotent: a second click is not an error, and re-stamping would
        # overwrite the recorded previous_status with "not_needed" and destroy
        # the only record of what to restore.
        return {
            "ok": True, "task_run_id": task_run_id, "action": "not_needed",
            "mode": "unchanged", "status": current,
        }

    try:
        _not_needed.write(run_dir, current, reason=reason, by="ide")
    except OSError as exc:
        return {
            "ok": False, "task_run_id": task_run_id,
            "error": f"{type(exc).__name__}: {exc}",
        }

    _writeback_terminal(
        db, task_run_id, status=NOT_NEEDED,
        notes=NOT_NEEDED_NOTE + (f": {reason}" if reason else ""),
    )
    return {
        "ok": True, "task_run_id": task_run_id, "action": "not_needed",
        "mode": "closed", "previous_status": current,
    }


def _close_dangling_node_events(db: StateDB, task_run_id: str) -> None:
    """Insert synthetic node_end(CRASH) for every node_start lacking a
    node_end. Without this, the UI DAG derives 'running' from the dangling
    node_start forever (observed run-1781081895-31571, 2026-06-10).

    Starts and ends are paired PER NODE ID IN TIME ORDER (not as sets): a node
    that ran twice (start, end, start) still has one unmatched start and gets
    exactly one synthetic end. Set-based matching saw an end for the id and
    left that second run open forever (4 runs, RB-34)."""
    import sqlite3

    db_path = str(db.db_path)
    now = int(time.time())
    con = sqlite3.connect(db_path, timeout=5.0)
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        rows = con.execute(
            """
            SELECT event_type, payload_json FROM run_events
             WHERE run_id = ? AND event_type IN ('node_start', 'node_end')
             ORDER BY created_at, rowid
            """,
            (task_run_id,),
        ).fetchall()
        # One entry per still-open start: node id → stack of node types. An end
        # consumes the most recent open start; every start left over is a run
        # that never reported its end and needs a synthetic one. Keeping a
        # stack (rather than a bool per id) is what lets a re-run be closed
        # while a balanced node gets nothing.
        open_starts: dict[str, list[str]] = {}
        for ev_type, pj in rows:
            try:
                p = json.loads(pj) if pj else {}
            except json.JSONDecodeError:
                p = {}
            nid = p.get("node_id")
            if not nid:
                continue
            if ev_type == "node_start":
                open_starts.setdefault(nid, []).append(p.get("node_type", ""))
            else:
                pending = open_starts.get(nid)
                if pending:
                    pending.pop()
                # An end with no matching start is ignored: there is no
                # "negative start" to close, and inventing one would pair a
                # later start against it.
        cols = {row[1] for row in con.execute("PRAGMA table_info(run_events)")}
        has_finish_reason = "finish_reason" in cols
        for nid, pending in open_starts.items():
            for i, ntype in enumerate(pending):
                payload = {
                    "node_id": nid,
                    "node_type": ntype,
                    "verdict": "CRASH",
                    "interrupted": True,
                    # ``interrupted`` is in run_events.finish_reason's own CHECK
                    # vocabulary, so the synthetic row is self-describing for
                    # every reader instead of relying on them to interpret
                    # ``verdict``. Without it a reader keyed on finish_reason
                    # alone saw "" and reported the node "done"
                    # (run-1791930074-82118).
                    "finish_reason": "interrupted",
                    "duration_ms": 0,
                }
                event_id = (
                    # The run id keeps the key unique when several runs that
                    # share a node id are closed in the same second (the run
                    # reaper); ``i`` keeps it unique within one close when the
                    # node ran more than once.
                    f"evt-node_end-{task_run_id}-{nid}-killclose-{now}-{i}"
                )
                if has_finish_reason:
                    # RB-34: write the indexed column, not just the payload —
                    # a NULL reason is invisible to triage/retry readers.
                    con.execute(
                        """
                        INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at, finish_reason)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (event_id, task_run_id, "node_end",
                         json.dumps(payload), now, "interrupted"),
                    )
                else:
                    con.execute(
                        """
                        INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (event_id, task_run_id, "node_end",
                         json.dumps(payload), now),
                    )
        con.commit()
    except sqlite3.OperationalError:
        # run_events may not exist on very old state.dbs — best-effort.
        pass
    finally:
        con.close()


# ════════════════════════════════════════════════════════════════════════════
# U2 — operator-steering HTTP write surface
# ════════════════════════════════════════════════════════════════════════════
# HTTP-write counterpart to lib/operator_steering.sh's `operator_steering_emit`
# (bash, same-host only) and the read-side MCP `get_operator_steering` tool.
# Lets an external driver (dashboard, product backend, SDK client) inject a
# mid-run steering message that the targeted agent role sees on its NEXT
# context_assemble pack — without shelling into the run host. The workspace is
# resolved by the request's home (X-Mini-Ork-Home), so a remote driver can
# point this at the run's own state.db.

_STEER_ROLES = {"planner", "implementer", "reviewer", "verifier", "any"}
_STEER_SEVERITIES = {"info", "warn", "critical"}


def steer_run(
    db: StateDB,
    task_run_id: str | None,
    message: str,
    role_target: str = "any",
    severity: str = "info",
    source: str = "http-api",
    confidence: float = 0.8,
    ttl_secs: int = 3600,
) -> dict[str, Any]:
    """Insert an operator_steering row (HITL steering injection).

    task_run_id=None targets the global queue (next planner dispatch). A named
    run that no longer exists returns not-found; a named terminal run is still
    rejected upstream by the caller if desired. Validates role_target/severity/
    confidence against the operator_steering CHECK constraints before writing.
    Returns {ok, steering_id, run_id, ...}.
    """
    import sqlite3

    msg = (message or "").strip()
    if not msg:
        return {"ok": False, "error": "message required"}
    if role_target not in _STEER_ROLES:
        return {"ok": False, "error": f"invalid role_target: {role_target!r}"}
    if severity not in _STEER_SEVERITIES:
        return {"ok": False, "error": f"invalid severity: {severity!r}"}
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        return {"ok": False, "error": "confidence must be a number"}
    if not (0.0 <= conf <= 1.0):
        return {"ok": False, "error": "confidence must be in [0,1]"}

    if task_run_id:
        tr = db.row("SELECT id FROM task_runs WHERE id = ?", (task_run_id,))
        if tr is None:
            return {"ok": False, "error": "task_run not found", "task_run_id": task_run_id}

    now_ms = int(time.time() * 1000)
    expires_ms = now_ms + max(1, int(ttl_secs)) * 1000
    con = sqlite3.connect(str(db.db_path), timeout=5.0)
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        cur = con.execute(
            """INSERT INTO operator_steering
                 (run_id, role_target, severity, message, source, confidence, created_at, expires_at)
               VALUES (NULLIF(?, ''), ?, ?, ?, NULLIF(?, ''), ?, ?, ?)""",
            (task_run_id or "", role_target, severity, msg, source or "", conf, now_ms, expires_ms),
        )
        con.commit()
        steering_id = cur.lastrowid
    finally:
        con.close()
    return {
        "ok": True,
        "steering_id": steering_id,
        "run_id": task_run_id,
        "role_target": role_target,
        "severity": severity,
        "expires_at": expires_ms,
    }


# ════════════════════════════════════════════════════════════════════════════
# U1 — detached run launch
# ════════════════════════════════════════════════════════════════════════════
# Non-blocking counterpart to mini_ork.client.MiniOrk.run (which blocks up to
# 1800s). Returns a run_id immediately; the caller observes progress via the
# SSE /api/v1/stream endpoint and drives the run via /steer + /stop. run_id is
# pre-minted here (matching bin/mini-ork's `run-$(date+%s)-$$` shape) and passed
# through MINI_ORK_RUN_ID so the returned id IS the id the run uses.

# Slug charset for recipe names + run ids. Deliberately excludes '/' and any
# '..' so a caller-supplied recipe/run_id can never traverse out of the
# runs-inbox when used as a filename, nor inject a path argument.
_SAFE_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


def _is_safe_token(s: str) -> bool:
    return bool(s) and ".." not in s and all(c in _SAFE_TOKEN_CHARS for c in s)


def _mini_ork_root() -> Path:
    env = os.environ.get("MINI_ORK_ROOT")
    if env:
        return Path(env).resolve()
    # mini_ork/web/control.py → parents[2] == repo root (bin/ lives there).
    return Path(__file__).resolve().parents[2]


def launch_run(
    home: Path,
    recipe: str,
    kickoff_markdown: str,
    run_id: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Spawn `bin/mini-ork run <recipe> <kickoff>` DETACHED; return run_id now.

    The kickoff markdown is written to <home>/runs-inbox/<run_id>.md. The spawn
    uses an argv list (no shell), so recipe/kickoff cannot inject commands;
    recipe + run_id are additionally validated against a safe token charset.
    Returns {ok, run_id, recipe, pid, kickoff_path, log_path}.
    """
    import secrets
    import subprocess

    rcp = (recipe or "").strip()
    if not _is_safe_token(rcp):
        return {"ok": False, "error": f"invalid recipe: {recipe!r}"}
    if not (kickoff_markdown or "").strip():
        return {"ok": False, "error": "kickoff_markdown required"}

    rid = (run_id or "").strip() or f"run-{int(time.time())}-{secrets.token_hex(3)}"
    if not _is_safe_token(rid):
        return {"ok": False, "error": f"invalid run_id: {rid!r}"}

    root = _mini_ork_root()
    bin_path = root / "bin" / "mini-ork"
    if not bin_path.is_file():
        return {"ok": False, "error": f"bin/mini-ork not found under {root}"}

    inbox = Path(home) / "runs-inbox"
    try:
        inbox.mkdir(parents=True, exist_ok=True)
        kickoff_path = inbox / f"{rid}.md"
        kickoff_path.write_text(kickoff_markdown, encoding="utf-8")
        log_path = inbox / f"{rid}.launch.log"
    except OSError as exc:
        return {"ok": False, "error": f"could not stage kickoff: {exc}"}

    env = dict(os.environ)
    env["MINI_ORK_RUN_ID"] = rid
    env["MINI_ORK_HOME"] = str(home)
    # Every DB resolver prefers MINI_ORK_DB over MINI_ORK_HOME, so setting only
    # the home left the child free to fall back to $(pwd)/.mini-ork/state.db —
    # the operator's live DB when the server runs from their checkout (RBF-27).
    # Pin the child to the same file its home already implies.
    env["MINI_ORK_DB"] = str(Path(home) / "state.db")
    # bin/mini-ork prefers MINI_ORK_PROJECT_HOME over MINI_ORK_HOME, and the
    # parent's value is whatever ITS cwd implied — e.g. <linked worktree>/.mini-ork,
    # which does not exist. Pin both to the home this run belongs to.
    env["MINI_ORK_PROJECT_HOME"] = str(home)
    env["MINI_ORK_ROOT"] = str(root)
    # Drop the parent's re-exec marker. If it leaked, bin/mini-ork's
    # _reexec_in_project_venv would skip the child's own venv re-exec and run on
    # whatever interpreter started it. The child sets the flag again itself when
    # it re-execs.
    env.pop("MINI_ORK_VENV_ACTIVE", None)
    blocked_premium: list[str] = []
    if extra_env:
        overrides = {str(k): str(v) for k, v in extra_env.items()}
        accept = (
            overrides.get("MO_ACCEPT_PREMIUM", env.get("MO_ACCEPT_PREMIUM", "")) not in ("", "0")
        )
        overrides, blocked_premium = gate_env(overrides, accept_premium=bool(accept))
        env.update(overrides)

    try:
        log_fh = open(log_path, "ab")  # noqa: SIM115 — handed to the child; closed in parent below
        # Spawn under the current interpreter, not the shebang's `env python3` —
        # the latter is whatever `python3` is first on PATH and on macOS is 3.9,
        # which trips bin/mini-ork's version check (mini_ork/web/control.py → ACP
        # agent dead-poll). sys.executable is the venv python in production.
        proc = subprocess.Popen(
            [sys.executable, str(bin_path), "run", rcp, str(kickoff_path)],
            cwd=str(root),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach from the server's process group
        )
        log_fh.close()
    except OSError as exc:
        return {"ok": False, "error": f"spawn failed: {exc}"}

    return {
        "ok": True,
        "run_id": rid,
        "recipe": rcp,
        "pid": proc.pid,
        "blocked_premium": blocked_premium,
        "kickoff_path": str(kickoff_path),
        "log_path": str(log_path),
    }
