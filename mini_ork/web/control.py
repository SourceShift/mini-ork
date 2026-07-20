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
import signal
import time
from datetime import datetime, timezone
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


def _close_dangling_node_events(db: StateDB, task_run_id: str) -> None:
    """Insert synthetic node_end(CRASH) for every node_start lacking a
    node_end. Without this, the UI DAG derives 'running' from the dangling
    node_start forever (observed run-1781081895-31571, 2026-06-10)."""
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
            """,
            (task_run_id,),
        ).fetchall()
        started: dict[str, str] = {}
        ended: set[str] = set()
        for ev_type, pj in rows:
            try:
                p = json.loads(pj) if pj else {}
            except json.JSONDecodeError:
                p = {}
            nid = p.get("node_id")
            if not nid:
                continue
            if ev_type == "node_start":
                started[nid] = p.get("node_type", "")
            else:
                ended.add(nid)
        for nid, ntype in started.items():
            if nid in ended:
                continue
            payload = {
                "node_id": nid,
                "node_type": ntype,
                "verdict": "CRASH",
                "interrupted": True,
                "duration_ms": 0,
            }
            con.execute(
                """
                INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (f"evt-node_end-{nid}-killclose-{now}", task_run_id, "node_end", json.dumps(payload), now),
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
    env["MINI_ORK_ROOT"] = str(root)
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items()})

    try:
        log_fh = open(log_path, "ab")  # noqa: SIM115 — handed to the child; closed in parent below
        proc = subprocess.Popen(
            [str(bin_path), "run", rcp, str(kickoff_path)],
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
        "kickoff_path": str(kickoff_path),
        "log_path": str(log_path),
    }
