"""Recovery admin: cancel + DAG projection (durable-dag E5).

Design source: ``internal-docs/architecture/2026-07-15-durable-dag-resume-design.md`` §8/§9/§11.

Two read/write-light operations that make recovery legible and controllable
WITHOUT touching the E1–E3 correctness logic (this module only reads their
tables and calls the E3 lease API as a client):

  * ``cancel_recovery(db, request_id)`` — cancel a pending recovery. Marks the
    ``recovery_requests`` row terminal (status=failed, failure_class=cancelled),
    releases the run lease so a fresh recovery can acquire it, and DOES NOT
    touch ``node_checkpoints`` — prior valid checkpoints stay reusable
    (E5 acceptance: "recover --cancel leaves prior checkpoints valid").

  * ``recovery_projection(db, run_id)`` — a DAG-shaped view assembled from
    ``node_checkpoints`` (completed nodes), ``node_attempts`` (per-node attempt
    history incl. failures), ``recovery_requests`` (the active/last recovery +
    next action), and ``run_leases`` (who owns the run). Read straight from the
    tables — never log scraping — so the web UI renders a recovered run as ONE
    DAG with nested attempts, not two disconnected runs.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from typing import Optional

__all__ = ["cancel_recovery", "recovery_projection"]

_BUSY_MS = 5000


def _log(msg: str) -> None:
    sys.stderr.write(f"recovery_admin: {msg}\n")


def _open(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=_BUSY_MS / 1000)
    con.execute(f"PRAGMA busy_timeout={_BUSY_MS}")
    con.row_factory = sqlite3.Row
    return con


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def cancel_recovery(db: str, request_id: str, *, now: Optional[int] = None) -> dict:
    """Cancel a pending/dispatched recovery request. Returns a result dict:
    ``{ok, request_id, previous_status, lease_released, checkpoints_preserved}``.

    Idempotent-ish: cancelling an already-closed request is a no-op success.
    Never invalidates node_checkpoints (the whole point — a cancel must not
    cost the operator the work already checkpointed)."""
    res = {"ok": False, "request_id": request_id, "previous_status": None,
           "lease_released": False, "checkpoints_preserved": True}
    if not db or not request_id or not os.path.isfile(db):
        _log("cancel_recovery: db + request_id required")
        return res
    ts = int(now) if now is not None else int(time.time())
    try:
        con = _open(db)
        try:
            if not _table_exists(con, "recovery_requests"):
                _log("cancel_recovery: recovery_requests table absent (pre-0052 DB)")
                return res
            row = con.execute(
                "SELECT run_id, status, owner_token FROM recovery_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                _log(f"cancel_recovery: no such request_id {request_id!r}")
                return res
            res["previous_status"] = row["status"]
            run_id, owner_token = row["run_id"], row["owner_token"]
            if row["status"] in ("completed", "failed"):
                # already terminal — nothing to cancel; still report success so
                # the CLI is idempotent.
                res["ok"] = True
                return res
            con.execute(
                "UPDATE recovery_requests SET status='failed', failure_class='cancelled', "
                "closed_at=? WHERE request_id=?",
                (ts, request_id),
            )
            # release the lease this recovery held (if any) so a new recovery
            # can acquire it. Direct DELETE keyed on the stored owner_token —
            # equivalent to lease.release_lease, done in-transaction here.
            if owner_token and _table_exists(con, "run_leases"):
                cur = con.execute(
                    "DELETE FROM run_leases WHERE run_id=? AND owner_token=?",
                    (run_id, owner_token),
                )
                res["lease_released"] = cur.rowcount == 1
            con.commit()
            res["ok"] = True
            return res
        finally:
            con.close()
    except sqlite3.Error as e:
        _log(f"cancel_recovery: {e}")
        return res


def recovery_projection(db: str, run_id: str) -> dict:
    """Assemble a DAG-shaped recovery view for ``run_id`` from the durable
    tables. Shape:

        {
          "run_id": ...,
          "nodes": [ { "node_id", "status", "reusable", "attempts": [
                         {"attempt_no","result","failure_class","started_at","ended_at"} ] } ],
          "active_recovery": {"request_id","status","from_node","dispatch_count",
                              "failure_class"} | None,
          "lease": {"owner_token","expires_at","live"} | None,
          "next_action": <str>,
        }

    Never raises: any error yields a minimal ``{"run_id", "nodes": [], ...}``
    so a UI caller can always render something."""
    out = {"run_id": run_id, "nodes": [], "active_recovery": None,
           "lease": None, "next_action": ""}
    if not db or not run_id or not os.path.isfile(db):
        return out
    now = int(time.time())
    try:
        con = _open(db)
        try:
            # completed nodes (checkpointed) + reusability proxy (status==success)
            ck = {}
            if _table_exists(con, "node_checkpoints"):
                for r in con.execute(
                    "SELECT node_id, status FROM node_checkpoints WHERE run_id=?", (run_id,)
                ):
                    ck[r["node_id"]] = r["status"]
            # per-node attempt history (incl. failures) — the nested attempts
            attempts: dict[str, list] = {}
            if _table_exists(con, "node_attempts"):
                for r in con.execute(
                    "SELECT node_id, attempt_no, result, failure_class, started_at, ended_at "
                    "FROM node_attempts WHERE run_id=? ORDER BY node_id, attempt_no", (run_id,)
                ):
                    attempts.setdefault(r["node_id"], []).append({
                        "attempt_no": r["attempt_no"], "result": r["result"],
                        "failure_class": r["failure_class"],
                        "started_at": r["started_at"], "ended_at": r["ended_at"],
                    })
            node_ids = sorted(set(ck) | set(attempts))
            for nid in node_ids:
                status = ck.get(nid) or (attempts.get(nid, [{}])[-1].get("result") or "unknown")
                out["nodes"].append({
                    "node_id": nid,
                    "status": status,
                    "reusable": ck.get(nid) == "success",
                    "attempts": attempts.get(nid, []),
                })
            # active/last recovery request
            if _table_exists(con, "recovery_requests"):
                rr = con.execute(
                    "SELECT request_id, status, from_node, dispatch_count, failure_class "
                    "FROM recovery_requests WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                if rr is not None:
                    out["active_recovery"] = {
                        "request_id": rr["request_id"], "status": rr["status"],
                        "from_node": rr["from_node"], "dispatch_count": rr["dispatch_count"],
                        "failure_class": rr["failure_class"],
                    }
            # lease ownership
            if _table_exists(con, "run_leases"):
                lr = con.execute(
                    "SELECT owner_token, expires_at FROM run_leases WHERE run_id=?", (run_id,)
                ).fetchone()
                if lr is not None:
                    out["lease"] = {
                        "owner_token": lr["owner_token"],
                        "expires_at": lr["expires_at"],
                        "live": int(lr["expires_at"]) > now,
                    }
        finally:
            con.close()
    except sqlite3.Error as e:
        _log(f"recovery_projection: {e}")
        return out
    out["next_action"] = _next_action(out)
    return out


def _next_action(view: dict) -> str:
    """A one-line operator hint derived from the projection."""
    rec = view.get("active_recovery")
    lease = view.get("lease")
    if rec and rec["status"] in ("pending", "dispatched"):
        if lease and lease.get("live"):
            return f"recovery in progress from node={rec['from_node']} (lease live)"
        return f"recovery {rec['status']} from node={rec['from_node']}; lease not live — re-run recover"
    failed = [n for n in view["nodes"] if n["status"] not in ("success", "skipped")]
    if failed:
        return f"failed node(s): {', '.join(n['node_id'] for n in failed)}; run `mini-ork recover <run_id>`"
    if view["nodes"]:
        return "all recorded nodes reusable; nothing to recover"
    return "no recorded nodes for this run"
