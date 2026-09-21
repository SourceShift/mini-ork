"""Reversible retirement lifecycle for the semantic memory win/loss ledger.

The semantic store (``mini_ork.memory.semantic``) already has a measured signal
for "this guidance is now wrong": the ``semantic_memory_uses`` retrieval ledger
feeds denormalized ``uses`` / ``wins`` counters, and ``_utility(uses, wins)``
turns them into a Laplace-smoothed win rate. This module layers a second,
*reversible* lifecycle on top of that evidence (arXiv 2608.18177):

  * ``retire`` — flag a memory obsolete with a mandatory reason + optional
    evidence pointer. A flag, never a DELETE: the row stays in the table.
  * ``reactivate`` — clear the flag, restoring the memory to active.
  * ``retirement_state`` — read the current flag state.
  * ``candidates`` — *report* obsolescence / recovery candidates against
    asymmetric hysteresis thresholds. It never auto-retires anything.

Two properties are load-bearing:

1. **Retirement is a move, not a delete.** The injection channel
   (``context_assembler.semantic_lessons_md``) re-``upsert``s every approved
   pattern keyed by ``pattern_id`` on every assembly. If retirement were a
   DELETE, the next injection would resurrect the entry. The retired row stays
   in the table and is filtered out of retrieval in ``semantic.py``; a repeat
   ``upsert`` of the same key must not clear the flag.

2. **Suppression happens at the read path.** ``retire`` / ``reactivate`` only
   write the three flag columns; the ``AND retired_at = 0`` filter lives in the
   retrieval queries in ``semantic.py``, so this module never has to reconcile
   the ledger itself.

The age-based TTL path (``mini_ork/dispatch/retention.py``) is orthogonal and
unchanged — this is a second lifecycle on top of it, not a replacement.
"""

from __future__ import annotations

import os
import time

from .semantic import _connect, _resolve_db_path, _utility

# Hysteresis thresholds. Asymmetric on purpose: a row must fall below ``enter``
# to be recommended for retirement and climb above ``exit`` to be recommended
# back. A symmetric (or inverted) configuration has no dead band, so an entry
# whose utility wavers near the boundary would oscillate between states.
RETIRE_ENTER_UTILITY = 0.25
RETIRE_EXIT_UTILITY = 0.45
RETIRE_MIN_USES = 4


def retire(
    memory_id: int,
    reason: str,
    evidence_ref: str = "",
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> bool:
    """Flag ``memory_id`` as retired, with a mandatory reason.

    ``reason`` must be a non-empty string — a retirement without a stated
    reason is the opaque delete this design exists to avoid, so it raises
    ``ValueError`` instead.

    Returns ``True`` iff a memory with that id exists (the state after the call
    is always "retired"); ``False`` when the id is unknown. Re-retiring an
    already-retired memory is a no-op that preserves the original
    ``retired_at`` / reason / evidence — retirement history is not rewritten by
    a second call.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    db = _resolve_db_path(db_path)
    conn = _connect(db)
    try:
        row = conn.execute(
            "SELECT id, retired_at FROM semantic_memory WHERE id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            return False
        if not row["retired_at"]:
            conn.execute(
                "UPDATE semantic_memory "
                "SET retired_at = ?, retire_reason = ?, retire_evidence = ? "
                "WHERE id = ?",
                (time.time(), reason, evidence_ref, memory_id),
            )
            conn.commit()
        return True
    finally:
        conn.close()


def reactivate(
    memory_id: int,
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> bool:
    """Clear the retirement flag, restoring ``memory_id`` to active.

    Returns ``True`` iff the id exists. Idempotent — reactivating an active
    memory returns ``True`` and changes nothing.
    """
    db = _resolve_db_path(db_path)
    conn = _connect(db)
    try:
        row = conn.execute(
            "SELECT id FROM semantic_memory WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE semantic_memory "
            "SET retired_at = 0, retire_reason = '', retire_evidence = '' "
            "WHERE id = ?",
            (memory_id,),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def retirement_state(
    memory_id: int,
    *,
    db_path: str | os.PathLike[str] | None = None,
) -> dict | None:
    """Return the retirement state of ``memory_id``, or ``None`` if unknown.

    Shape: ``{"memory_id", "retired", "retired_at", "reason", "evidence"}``.
    """
    db = _resolve_db_path(db_path)
    conn = _connect(db)
    try:
        row = conn.execute(
            "SELECT retired_at, retire_reason, retire_evidence "
            "FROM semantic_memory WHERE id = ?",
            (memory_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "memory_id": int(memory_id),
        "retired": bool(row["retired_at"]),
        "retired_at": float(row["retired_at"]),
        "reason": str(row["retire_reason"]),
        "evidence": str(row["retire_evidence"]),
    }


def candidates(
    scope: str,
    *,
    db_path: str | os.PathLike[str] | None = None,
    enter: float = RETIRE_ENTER_UTILITY,
    exit: float = RETIRE_EXIT_UTILITY,
    min_uses: int = RETIRE_MIN_USES,
) -> list[dict]:
    """Report obsolescence / recovery candidates in ``scope``.

    Reads the rows in ``scope`` and returns, sorted by ``utility`` ascending:

      * every **active** row with ``uses >= min_uses`` **and**
        ``utility < enter`` → recommendation ``"retire"``;
      * every **retired** row with ``utility > exit`` → recommendation
        ``"reactivate"``.

    Both directions in one call — that is the hysteresis: a row must fall below
    ``enter`` to be recommended for retirement and climb above ``exit`` to be
    recommended back. Raises ``ValueError`` when ``exit <= enter``, because the
    asymmetry is the whole mechanism and a non-asymmetric configuration
    oscillates.

    This reports only; it never auto-retires anything.
    """
    if exit <= enter:
        raise ValueError(
            f"exit must be > enter for hysteresis (got exit={exit}, enter={enter})"
        )
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("scope must be a non-empty string")

    db = _resolve_db_path(db_path)
    conn = _connect(db)
    try:
        rows = conn.execute(
            "SELECT id, text, uses, wins, retired_at "
            "FROM semantic_memory WHERE scope = ?",
            (scope,),
        ).fetchall()
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        uses = int(r["uses"])
        wins = int(r["wins"])
        utility = _utility(uses, wins)
        if not r["retired_at"]:
            if uses >= min_uses and utility < enter:
                out.append({
                    "memory_id": int(r["id"]),
                    "text": r["text"],
                    "uses": uses,
                    "wins": wins,
                    "utility": utility,
                    "state": "active",
                    "recommendation": "retire",
                })
        elif utility > exit:
            out.append({
                "memory_id": int(r["id"]),
                "text": r["text"],
                "uses": uses,
                "wins": wins,
                "utility": utility,
                "state": "retired",
                "recommendation": "reactivate",
            })
    out.sort(key=lambda d: d["utility"])
    return out
