"""HITL steering checkpoints — Python port of lib/steering_checkpoint.sh.

A checkpoint lets a recipe pause an in-flight run for human steering, then
resume once steering arrives. It composes two primitives that already exist:
``operator_steering`` (the steering message carrier) and the dispatcher's
plan-status gate (the pause/resume), exactly like ``plan_status=needs_answers``.

Co-existence model (strangler-fig): bash ``lib/steering_checkpoint.sh`` is the
authoritative source and stays untouched. This module mirrors its public API
1:1 so Python callers get an in-process surface and
``tests/unit/test_steering_checkpoint_py.py`` gets a stable target to byte-diff
against the live bash subprocess.

Shared-DB invariant: the bash library ``source``s ``operator_steering.sh`` to
inherit ``_operator_steering_db`` / ``_operator_steering_now_ms``. The port
reuses ``mini_ork.ported.operator_steering._resolve_db`` / ``._now_ms`` for the
same reason — ``has_unconsumed`` must read the exact ``state.db`` that
``operator_steering.emit`` / ``.fetch_for`` write to, or it reads an empty DB.
If ``operator_steering`` is unavailable at import time, we fall through to the
same env-only path bash uses when the ``declare -F`` probe misses.

Public API (bash function → Python):
  mo_steering_has_unconsumed   → has_unconsumed(run_id, role='any') -> int
  mo_steering_checkpoint_mark  → mark(run_id, node_id, reason=None) -> None
  mo_steering_checkpoint_clear → clear(run_id) -> None
  mo_steering_checkpoint_status→ status(run_id) -> dict
  mo_steering_checkpoint_gate  → gate(run_id, node_id='checkpoint', role='any') -> int
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import time

__all__ = [
    "has_unconsumed",
    "mark",
    "clear",
    "status",
    "gate",
]


# Reuse the db-path + now-ms helpers from the operator_steering port, mirroring
# bash's `. operator_steering.sh` + `declare -F _operator_steering_db` inheritance.
# If the port is unavailable at import time, fall through to the same env-only
# path bash uses when the declare probe misses.
try:
    from mini_ork.ported.operator_steering import _resolve_db as _op_resolve_db
    from mini_ork.ported.operator_steering import _now_ms as _op_now_ms
except ImportError:  # pragma: no cover - env-only fallback
    _op_resolve_db = None
    _op_now_ms = None


def _db() -> str:
    """Mirror ``_mo_steering_db``: reuse ``_operator_steering_db`` when present,
    else the env fallback ``${MINI_ORK_DB:-${MINI_ORK_HOME:-$(pwd)/.mini-ork}/state.db}``."""
    if _op_resolve_db is not None:
        return _op_resolve_db()
    if os.environ.get("MINI_ORK_DB"):
        return os.environ["MINI_ORK_DB"]
    if os.environ.get("MINI_ORK_HOME"):
        return os.path.join(os.environ["MINI_ORK_HOME"], "state.db")
    return os.path.join(os.getcwd(), ".mini-ork", "state.db")


def _now_ms() -> int:
    """Mirror ``_mo_steering_now_ms``: reuse ``_operator_steering_now_ms`` when
    present, else ``int(time.time() * 1000)``."""
    if _op_now_ms is not None:
        return _op_now_ms()
    return int(time.time() * 1000)


def _log(level: str, msg: str) -> None:
    """Mirror ``_mo_steering_log``: one-line JSON to stderr."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(
        '{"level":"%s","subsystem":"steering_checkpoint","ts":"%s","msg":"%s"}\n'
        % (level, ts, msg)
    )


def _run_dir(run_id: str) -> str:
    """Mirror ``_mo_steering_run_dir``: MINI_ORK_RUN_DIR when set+isdir, else
    ``${MINI_ORK_HOME:-.mini-ork}/runs/<run_id>``."""
    run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
    if run_dir and os.path.isdir(run_dir):
        return run_dir
    home = os.environ.get("MINI_ORK_HOME") or ".mini-ork"
    return os.path.join(home, "runs", run_id)


def _sentinel_path(run_id: str) -> str:
    return os.path.join(_run_dir(run_id), ".steering-checkpoint")


def _marker_path(run_id: str) -> str:
    return os.path.join(_run_dir(run_id), ".steering-checkpoint.json")


def has_unconsumed(run_id: str, role: str = "any") -> int:
    """Mirror ``mo_steering_has_unconsumed``.

    rc=0 when an unconsumed, unexpired steering row exists for this run (or the
    global NULL-run queue) addressed to ``role`` (or ``"any"`` on either side);
    rc=1 otherwise. rc=2 when ``run_id`` is empty (bash: "run_id required").
    rc=1 when the DB file is absent (bash: ``[ -f "$_db" ] || return 1``).
    """
    if not run_id:
        _log("error", "mo_steering_has_unconsumed: run_id required")
        return 2

    db = _db()
    if not os.path.isfile(db):
        return 1  # no db → nothing to consume

    now = _now_ms()
    con = sqlite3.connect(db, timeout=5.0)
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        row = con.execute(
            """SELECT 1 FROM operator_steering
                WHERE consumed_at IS NULL
                  AND expires_at > ?
                  AND (run_id = ? OR run_id IS NULL)
                  AND (role_target = ? OR role_target = 'any' OR ? = 'any')
                LIMIT 1""",
            (now, run_id, role, role),
        ).fetchone()
    finally:
        con.close()
    return 0 if row else 1


def mark(run_id: str, node_id: str, reason: str | None = None) -> None:
    """Mirror ``mo_steering_checkpoint_mark``: truncate the sentinel and write
    the ``.steering-checkpoint.json`` marker.

    Empty ``run_id`` is a no-op (bash returns rc=2 without writing). The bash
    ``mark`` swallows mkdir/write failures (``2>/dev/null || true`` on both the
    ``mkdir -p`` and the python heredoc), so the port mirrors that narrowly with
    an OSError guard — this is faithful bash behavior, not a recovery fallback.
    """
    if not run_id:
        return
    run_dir = _run_dir(run_id)
    try:
        os.makedirs(run_dir, exist_ok=True)
    except OSError:
        pass
    sentinel = _sentinel_path(run_id)
    marker = _marker_path(run_id)
    now = _now_ms()
    try:
        with open(sentinel, "w"):
            pass  # `: > "$sentinel"` — truncate/create, empty content
        with open(marker, "w") as fh:
            json.dump({
                "awaiting_steering": True,
                "run_id": run_id,
                "node_id": node_id,
                "reason": reason or None,
                "requested_at_ms": int(now),
            }, fh)
    except OSError:
        pass
    _log("info", f"checkpoint awaiting steering: run={run_id} node={node_id}")


def clear(run_id: str) -> None:
    """Mirror ``mo_steering_checkpoint_clear``: remove the sentinel + marker.

    Empty ``run_id`` is a no-op (bash returns rc=2 without removing). Missing
    files are ignored (bash: ``rm -f … 2>/dev/null || true``).
    """
    if not run_id:
        return
    for path in (_sentinel_path(run_id), _marker_path(run_id)):
        try:
            os.remove(path)
        except OSError:
            pass


def status(run_id: str) -> dict:
    """Mirror ``mo_steering_checkpoint_status``: emit the awaiting-state JSON.

    - empty ``run_id`` → ``{"awaiting": False}``
    - sentinel + marker present → marker dict + ``awaiting=True`` + ``sentinel_path``
    - sentinel present, marker absent → ``{"awaiting": True, "sentinel_path": …}``
    - no sentinel → ``{"awaiting": False}``
    """
    if not run_id:
        return {"awaiting": False}
    sentinel = _sentinel_path(run_id)
    marker = _marker_path(run_id)
    if os.path.isfile(sentinel):
        if os.path.isfile(marker):
            with open(marker) as fh:
                m = json.load(fh)
            m["awaiting"] = True
            m["sentinel_path"] = sentinel
            return m
        return {"awaiting": True, "sentinel_path": sentinel}
    return {"awaiting": False}


def gate(run_id: str, node_id: str = "checkpoint", role: str = "any") -> int:
    """Mirror ``mo_steering_checkpoint_gate``: the composite the dispatcher calls.

    rc=0 → steering present (marker cleared), proceed.
    rc=2 → no steering yet (marker written), pause the run.
    rc=2 also when ``run_id`` is empty (bash: "run_id required").
    """
    if not run_id:
        _log("error", "gate: run_id required")
        return 2
    if has_unconsumed(run_id, role) == 0:
        clear(run_id)
        _log("info", f"checkpoint satisfied: run={run_id} node={node_id}")
        return 0
    mark(run_id, node_id, "awaiting human steering")
    return 2
