"""Cross-epic dependency graph — Python port of lib/epic_graph.sh.

Faithful semantics: hard deps block, soft/informational don't; ready-set is
status='not started' AND no unresolved hard dep, oldest-first; on_done resolves
outgoing edges then flips fully-unblocked downstream 'blocked' epics to
'not started'. This is the module the concurrent scheduler pools over.
"""
from __future__ import annotations

import os
import sqlite3


def _db_path(db: str | None) -> str:
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB")
    if env:
        return env
    home = os.environ.get("MINI_ORK_HOME", ".mini-ork")
    return os.path.join(home, "state.db")


def _conn(db: str | None) -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(db))
    con.execute("PRAGMA busy_timeout=5000")
    return con


def add_dep(from_id: str, to_id: str, kind: str = "hard",
            db: str | None = None) -> None:
    """Register an edge: from_id must reach 'done' before to_id can dispatch."""
    con = _conn(db)
    try:
        con.execute(
            "INSERT OR IGNORE INTO epic_dependencies(from_epic_id, to_epic_id, kind) "
            "VALUES(?,?,?)", (from_id, to_id, kind))
        con.commit()
    finally:
        con.close()


def deps_met(epic_id: str, db: str | None = None) -> bool:
    """True when every HARD dep of the epic is resolved."""
    con = _conn(db)
    try:
        unmet = con.execute(
            "SELECT COUNT(*) FROM epic_dependencies "
            "WHERE to_epic_id=? AND kind='hard' AND resolved_at IS NULL",
            (epic_id,)).fetchone()[0]
        return unmet == 0
    finally:
        con.close()


def ready_now(db: str | None = None) -> list[str]:
    """Epic ids with status='not started', unarchived, all hard deps met —
    oldest first (fairness)."""
    con = _conn(db)
    try:
        rows = con.execute(
            """SELECT e.id FROM epics e
                WHERE e.status = 'not started' AND e.archived_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM epic_dependencies d
                       WHERE d.to_epic_id = e.id AND d.kind = 'hard'
                         AND d.resolved_at IS NULL)
             ORDER BY e.created_at ASC""").fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error:
        # Fresh db without the epics/epic_dependencies tables → no ready epics,
        # matching bash's tolerant `sqlite3 … 2>/dev/null` (crash-free empty result).
        return []
    finally:
        con.close()


def mark_ready(epic_id: str, db: str | None = None) -> None:
    """Flip 'blocked' -> 'not started' (idempotent)."""
    con = _conn(db)
    try:
        con.execute("UPDATE epics SET status='not started' "
                    "WHERE id=? AND status='blocked'", (epic_id,))
        con.commit()
    finally:
        con.close()


def on_done(done_id: str, db: str | None = None) -> None:
    """Resolve outgoing edges of a completed epic, then unblock any downstream
    epic whose remaining hard deps are all met."""
    con = _conn(db)
    try:
        con.execute(
            "UPDATE epic_dependencies "
            "SET resolved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE from_epic_id=? AND resolved_at IS NULL", (done_id,))
        downstream = [r[0] for r in con.execute(
            "SELECT DISTINCT to_epic_id FROM epic_dependencies WHERE from_epic_id=?",
            (done_id,)).fetchall()]
        for ep in downstream:
            unmet = con.execute(
                "SELECT COUNT(*) FROM epic_dependencies "
                "WHERE to_epic_id=? AND kind='hard' AND resolved_at IS NULL",
                (ep,)).fetchone()[0]
            if unmet == 0:
                con.execute("UPDATE epics SET status='not started' "
                            "WHERE id=? AND status='blocked'", (ep,))
        con.commit()
    finally:
        con.close()
