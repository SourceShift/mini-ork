"""Single-writer lease + fencing + idempotent recovery requests (E3).

Design source: ``internal-docs/architecture/2026-07-15-durable-dag-resume-design.md``
§5 (failure-class state machine), §7 (single-writer ownership / fencing tokens).
Schema: ``db/migrations/0052_run_leases_recovery_requests.sql``.

Two concerns, one module because they share the same concurrency invariant:

1. **Lease + fencing** (``run_leases``). Before any recovery dispatches a node
   or publishes a checkpoint it must hold the run's lease. A lease is an
   ``owner_token`` with an ``expires_at``. Any checkpoint/terminal write that
   presents a token which is not the CURRENT LIVE holder is **rejected** —
   this is what stops a stale worker (whose token expired and was re-acquired
   by a newer recovery) from publishing over the top of live work
   (design §7). Concurrency is serialized by ``BEGIN IMMEDIATE`` so two
   simultaneous acquirers cannot both win (scenario 6).

2. **Idempotent recovery requests** (``recovery_requests``). The tuple
   ``(run_id, from_node, strategy)`` is a unique key; a duplicate
   ``request_recovery`` for the same tuple returns the EXISTING request
   instead of dispatching a second time. Budget is bounded per request
   (``budget_usd``) so ``provider_limit``/``repair`` recoveries can never
   become an unbounded auto-retry loop (design §5).

Time is injectable (``now=``) so tests are deterministic; production callers
omit it and get ``int(time.time())``. Nothing here raises for a normal
"can't acquire / not the holder" outcome — those return ``None``/``False`` so
callers branch on them; only genuinely broken input (missing db) surfaces.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import sys
import time
from typing import Optional

__all__ = [
    "FenceError",
    "mint_token",
    "acquire_lease",
    "refresh_lease",
    "release_lease",
    "is_lease_holder",
    "fence_or_reject",
    "current_lease",
    "request_recovery",
    "find_recovery",
    "get_recovery",
    "mark_dispatched",
    "close_recovery",
    "can_dispatch",
    "DEFAULT_LEASE_TTL_S",
    "DEFAULT_BUDGET_USD",
]

DEFAULT_LEASE_TTL_S = 900       # 15 min; refresh() extends while the holder lives
DEFAULT_BUDGET_USD = 5.00
_BUSY_MS = 5000


class FenceError(Exception):
    """Raised only by ``fence_or_reject`` when a caller opts into hard fencing."""


def _log(msg: str) -> None:
    sys.stderr.write(f"lease: {msg}\n")


def _now(now: Optional[int]) -> int:
    return int(now) if now is not None else int(time.time())


def mint_token() -> str:
    """Unguessable owner token. 32 hex chars = 128 bits — a stale worker cannot
    forge the newer holder's token."""
    return secrets.token_hex(16)


def _open(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=_BUSY_MS / 1000)
    con.execute(f"PRAGMA busy_timeout={_BUSY_MS}")
    return con


def lease_tables_present(db: str) -> bool:
    """True iff migration 0052 has run (run_leases + recovery_requests exist).

    Recovery on a legacy DB (pre-0052) must proceed WITHOUT a lease rather
    than misread a missing table as "someone else holds the lease". Callers
    gate all E3 wiring on this so an un-migrated consumer keeps E2 behavior.
    """
    if not db or not os.path.isfile(db):
        return False
    try:
        con = _open(db)
        try:
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('run_leases','recovery_requests')"
            ).fetchall()
            return {r[0] for r in rows} >= {"run_leases", "recovery_requests"}
        finally:
            con.close()
    except sqlite3.Error:
        return False


# ── lease + fencing ───────────────────────────────────────────────────────

def acquire_lease(
    db: str,
    run_id: str,
    *,
    owner_token: Optional[str] = None,
    ttl_s: int = DEFAULT_LEASE_TTL_S,
    now: Optional[int] = None,
) -> Optional[str]:
    """Acquire (or re-acquire) the single-writer lease for ``run_id``.

    Returns the ``owner_token`` on success, ``None`` if a *live* lease is held
    by a different owner (the safe "someone else owns this" answer — the
    caller must not proceed).

    Rules:
      * no row            → INSERT, acquired.
      * row expired       → steal it (atomic UPDATE guarded on ``expires_at<=now``).
      * live + same token → re-entrant refresh, acquired.
      * live + other owner → ``None`` (blocked).

    ``BEGIN IMMEDIATE`` takes the write lock up-front so two concurrent
    acquirers serialize: the loser sees the winner's fresh row and returns
    ``None`` (design §7, scenario 6).
    """
    if not db or not run_id:
        _log("acquire_lease: db and run_id required")
        return None
    ts = _now(now)
    token = owner_token or mint_token()
    exp = ts + max(1, int(ttl_s))
    con = None
    try:
        con = _open(db)
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT owner_token, expires_at FROM run_leases WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO run_leases(run_id, owner_token, acquired_at, expires_at, renewed_at) "
                "VALUES (?,?,?,?,?)",
                (run_id, token, ts, exp, ts),
            )
            con.execute("COMMIT")
            return token
        cur_owner, cur_exp = row[0], int(row[1])
        if cur_exp <= ts:  # expired → steal
            cur = con.execute(
                "UPDATE run_leases SET owner_token=?, acquired_at=?, expires_at=?, renewed_at=? "
                "WHERE run_id=? AND expires_at<=?",
                (token, ts, exp, ts, run_id, ts),
            )
            if cur.rowcount == 1:
                con.execute("COMMIT")
                return token
            con.execute("ROLLBACK")
            return None
        if cur_owner == token:  # re-entrant → refresh
            con.execute(
                "UPDATE run_leases SET expires_at=?, renewed_at=? WHERE run_id=? AND owner_token=?",
                (exp, ts, run_id, token),
            )
            con.execute("COMMIT")
            return token
        # live lease, different owner → blocked
        con.execute("ROLLBACK")
        return None
    except sqlite3.Error as e:
        _log(f"acquire_lease: {e}")
        if con is not None:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
        return None
    finally:
        if con is not None:
            con.close()


def refresh_lease(
    db: str, run_id: str, owner_token: str, *, ttl_s: int = DEFAULT_LEASE_TTL_S, now: Optional[int] = None
) -> bool:
    """Extend the lease while still alive. False if the token is not the live
    holder (expired or superseded) — the caller has lost ownership."""
    if not db or not run_id or not owner_token:
        return False
    ts = _now(now)
    exp = ts + max(1, int(ttl_s))
    try:
        con = _open(db)
        try:
            cur = con.execute(
                "UPDATE run_leases SET expires_at=?, renewed_at=? "
                "WHERE run_id=? AND owner_token=? AND expires_at>?",
                (exp, ts, run_id, owner_token, ts),
            )
            con.commit()
            return cur.rowcount == 1
        finally:
            con.close()
    except sqlite3.Error as e:
        _log(f"refresh_lease: {e}")
        return False


def release_lease(db: str, run_id: str, owner_token: str) -> bool:
    """Release the lease (only the holder can). False if not the holder."""
    if not db or not run_id or not owner_token:
        return False
    try:
        con = _open(db)
        try:
            cur = con.execute(
                "DELETE FROM run_leases WHERE run_id=? AND owner_token=?",
                (run_id, owner_token),
            )
            con.commit()
            return cur.rowcount == 1
        finally:
            con.close()
    except sqlite3.Error as e:
        _log(f"release_lease: {e}")
        return False


def is_lease_holder(db: str, run_id: str, owner_token: str, *, now: Optional[int] = None) -> bool:
    """True iff ``owner_token`` is the CURRENT LIVE holder of ``run_id``'s lease.

    This is the fence check every checkpoint/terminal write consults. A token
    that was valid but is now expired, or was superseded by a newer acquire,
    returns False — the write must be rejected.
    """
    if not db or not run_id or not owner_token:
        return False
    ts = _now(now)
    try:
        con = _open(db)
        try:
            row = con.execute(
                "SELECT 1 FROM run_leases WHERE run_id=? AND owner_token=? AND expires_at>?",
                (run_id, owner_token, ts),
            ).fetchone()
            return row is not None
        finally:
            con.close()
    except sqlite3.Error as e:
        _log(f"is_lease_holder: {e}")
        return False


def fence_or_reject(db: str, run_id: str, owner_token: str, *, now: Optional[int] = None) -> None:
    """Raise ``FenceError`` if ``owner_token`` is not the live holder. For
    callers that prefer an exception over a bool at the write seam."""
    if not is_lease_holder(db, run_id, owner_token, now=now):
        raise FenceError(f"fence rejected: {owner_token!r} is not the live holder of run {run_id!r}")


def current_lease(db: str, run_id: str) -> Optional[dict]:
    """Inspect the lease row (owner_token, acquired_at, expires_at, renewed_at)
    or None. For ``recover --status`` and diagnostics."""
    if not db or not run_id:
        return None
    try:
        con = _open(db)
        try:
            row = con.execute(
                "SELECT owner_token, acquired_at, expires_at, renewed_at FROM run_leases WHERE run_id=?",
                (run_id,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {
        "owner_token": row[0],
        "acquired_at": int(row[1]),
        "expires_at": int(row[2]),
        "renewed_at": int(row[3]),
    }


# ── idempotent recovery requests ──────────────────────────────────────────

def request_recovery(
    db: str,
    run_id: str,
    from_node: str,
    strategy: str,
    *,
    budget_usd: float = DEFAULT_BUDGET_USD,
    payload_json: Optional[str] = None,
    now: Optional[int] = None,
) -> Optional[tuple[str, bool]]:
    """Idempotently register a recovery request.

    Returns ``(request_id, created)``: ``created`` True if this call minted the
    row, False if an equivalent ``(run_id, from_node, strategy)`` request
    already existed (the returned id is that pre-existing request — the node
    runs once). Returns ``None`` only on a hard DB error.

    Two concurrent identical requests: the unique index
    ``uq_recovery_requests_idem`` makes the second INSERT a no-op, and the
    follow-up SELECT returns the winner's id — so both callers converge on one
    request (scenario 6, the idempotency half).
    """
    if not db or not run_id or not from_node or not strategy:
        _log("request_recovery: run_id, from_node, strategy required")
        return None
    ts = _now(now)
    request_id = mint_token()
    try:
        con = _open(db)
        try:
            cur = con.execute(
                "INSERT OR IGNORE INTO recovery_requests"
                "(request_id, run_id, from_node, strategy, status, budget_usd, cost_usd, "
                " dispatch_count, created_at, payload_json) "
                "VALUES (?,?,?,?, 'pending', ?, 0.0, 0, ?, ?)",
                (request_id, run_id, from_node, strategy, float(budget_usd), ts, payload_json),
            )
            con.commit()
            if cur.rowcount == 1:
                return (request_id, True)
            # collided on the idempotency index → return the existing row
            row = con.execute(
                "SELECT request_id FROM recovery_requests "
                "WHERE run_id=? AND from_node=? AND strategy=?",
                (run_id, from_node, strategy),
            ).fetchone()
            if row is None:
                return None
            return (row[0], False)
        finally:
            con.close()
    except sqlite3.Error as e:
        _log(f"request_recovery: {e}")
        return None


def find_recovery(db: str, run_id: str, from_node: str, strategy: str) -> Optional[dict]:
    """The recovery request for this tuple, or None."""
    if not db:
        return None
    try:
        con = _open(db)
        try:
            row = con.execute(
                "SELECT request_id, status, failure_class, budget_usd, cost_usd, dispatch_count, owner_token "
                "FROM recovery_requests WHERE run_id=? AND from_node=? AND strategy=?",
                (run_id, from_node, strategy),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return _row_to_recovery(row)


def get_recovery(db: str, request_id: str) -> Optional[dict]:
    if not db or not request_id:
        return None
    try:
        con = _open(db)
        try:
            row = con.execute(
                "SELECT request_id, status, failure_class, budget_usd, cost_usd, dispatch_count, owner_token "
                "FROM recovery_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return _row_to_recovery(row)


def _row_to_recovery(row) -> Optional[dict]:
    if row is None:
        return None
    return {
        "request_id": row[0],
        "status": row[1],
        "failure_class": row[2],
        "budget_usd": float(row[3]) if row[3] is not None else 0.0,
        "cost_usd": float(row[4]) if row[4] is not None else 0.0,
        "dispatch_count": int(row[5]) if row[5] is not None else 0,
        "owner_token": row[6],
    }


def can_dispatch(db: str, request_id: str, *, projected_cost_usd: float = 0.0) -> bool:
    """True iff dispatching (adding ``projected_cost_usd``) stays within the
    request's budget AND the request is still open. Budget bound = the design's
    "auto-retry must never be unbounded" guard, made a hard gate."""
    rec = get_recovery(db, request_id)
    if rec is None:
        return False
    if rec["status"] in ("completed", "failed"):
        return False
    return (rec["cost_usd"] + max(0.0, float(projected_cost_usd))) <= rec["budget_usd"]


def mark_dispatched(
    db: str,
    request_id: str,
    *,
    owner_token: str,
    cost_usd: float = 0.0,
    now: Optional[int] = None,
) -> bool:
    """Record that the recovery dispatched once. Increments ``dispatch_count``,
    adds ``cost_usd``, stamps the fencing ``owner_token``. Rejected (False) if
    the request is already closed or the new cost would exceed budget — so a
    caller cannot spin an unbounded retry loop past the budget ceiling."""
    if not db or not request_id or not owner_token:
        return False
    if not can_dispatch(db, request_id, projected_cost_usd=cost_usd):
        return False
    ts = _now(now)
    try:
        con = _open(db)
        try:
            cur = con.execute(
                "UPDATE recovery_requests "
                "SET status='dispatched', dispatch_count=dispatch_count+1, "
                "    cost_usd=cost_usd+?, owner_token=?, last_dispatched_at=? "
                "WHERE request_id=? AND status IN ('pending','dispatched')",
                (max(0.0, float(cost_usd)), owner_token, ts, request_id),
            )
            con.commit()
            return cur.rowcount == 1
        finally:
            con.close()
    except sqlite3.Error as e:
        _log(f"mark_dispatched: {e}")
        return False


def close_recovery(
    db: str,
    request_id: str,
    *,
    status: str,
    failure_class: Optional[str] = None,
    cost_usd: Optional[float] = None,
    now: Optional[int] = None,
) -> bool:
    """Close a recovery request (``completed`` | ``failed``)."""
    if status not in ("completed", "failed"):
        _log(f"close_recovery: status must be completed|failed, got {status!r}")
        return False
    ts = _now(now)
    try:
        con = _open(db)
        try:
            if cost_usd is not None:
                cur = con.execute(
                    "UPDATE recovery_requests SET status=?, failure_class=?, cost_usd=?, closed_at=? "
                    "WHERE request_id=?",
                    (status, failure_class, float(cost_usd), ts, request_id),
                )
            else:
                cur = con.execute(
                    "UPDATE recovery_requests SET status=?, failure_class=?, closed_at=? "
                    "WHERE request_id=?",
                    (status, failure_class, ts, request_id),
                )
            con.commit()
            return cur.rowcount == 1
        finally:
            con.close()
    except sqlite3.Error as e:
        _log(f"close_recovery: {e}")
        return False
