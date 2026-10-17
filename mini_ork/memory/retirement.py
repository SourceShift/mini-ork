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

import json
import os
import sqlite3
import time

from mini_ork.learning import code_findings

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


# ── report-only candidates for themes and learned rules (LF-R13 §5) ─────────
# Both readers are strictly read-only: they open the DB with ``mode=ro`` (never
# ``semantic._connect``, which bootstraps schema and commits) and never call
# ``retire``/``reactivate``. A missing table or column is ``[]``, never a raise:
# the CLI that surfaces them always exits 0.

THEME_CANDIDATE_MAX_AGE_DAYS = 90


def _connect_readonly(db_path: str | os.PathLike[str] | None) -> sqlite3.Connection | None:
    """A read-only connection, or ``None`` when the DB does not exist.

    No schema bootstrap: a report must be able to run against an old (or empty)
    database without leaving anything behind.
    """
    path = _resolve_db_path(db_path)
    if not path or not os.path.isfile(path):
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def theme_candidates(
    *,
    db_path: str | os.PathLike[str] | None = None,
    max_age_days: int = THEME_CANDIDATE_MAX_AGE_DAYS,
    now: int | None = None,
) -> list[dict]:
    """Report verified ``lesson_themes`` rows not seen within ``max_age_days``.

    A theme is a retirement candidate when its ``last_seen`` is older than the
    window ending at ``now``: ``last_seen < now - max_age_days * 86400``. A
    larger ``max_age_days`` therefore admits *fewer* rows — the window reaches
    further back from today, and only themes older than it are reported.
    ``last_seen`` is the only signal a theme carries (no win/loss counter), so
    it is reported as evidence. ``max_age_days <= 0`` disables the check and
    returns ``[]``. Sorted by ``last_seen`` ascending (oldest first).
    Reports only — no write-back.
    """
    if max_age_days <= 0:
        return []
    if now is None:
        now = int(time.time())
    con = _connect_readonly(db_path)
    if con is None:
        return []
    cutoff = int(now) - int(max_age_days) * 86400
    try:
        rows = con.execute(
            "SELECT theme_id, kind, representative, n_runs, last_seen, status "
            "FROM lesson_themes "
            "WHERE status = 'verified' AND last_seen IS NOT NULL AND last_seen < ? "
            "ORDER BY last_seen ASC",
            (cutoff,),
        ).fetchall()
    except sqlite3.Error:  # missing table or column (pre-last_seen schema)
        return []
    finally:
        con.close()
    out: list[dict] = []
    for r in rows:
        last_seen = int(r["last_seen"])
        days = max(0, (int(now) - last_seen) // 86400)
        out.append({
            "kind": "theme",
            "theme_id": r["theme_id"],
            "representative": r["representative"],
            "n_runs": int(r["n_runs"] or 0),
            "last_seen": last_seen,
            "state": "verified",
            "recommendation": "retire",
            "evidence": f"last seen {days} days ago",
        })
    return out


def _source_ids(raw) -> list[int]:
    """``source_ids`` JSON text → a non-empty list of integer finding ids.

    Anything unparsable, non-list, empty, or carrying no integer id is skipped
    (``[]``), never raised.
    """
    if raw is None:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    ids: list[int] = []
    for value in data:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids


def rule_candidates(
    *,
    db_path: str | os.PathLike[str] | None = None,
    now: int | None = None,
) -> list[dict]:
    """Report learned, active preference rows whose every source finding is stale.

    A rule is a *learned* preference (``origin='learned'``, ``active=1``) derived
    from ``code_findings`` ids in ``source_ids``. When none of those findings is
    fresh under R11a's ``annotate_staleness``, the rule is reported for
    retirement. Rows with unparsable/empty ``source_ids``, or a missing
    preference/findings table, are skipped. Reports only — no writes.
    """
    if now is None:
        now = int(time.time())
    con = _connect_readonly(db_path)
    if con is None:
        return []
    try:
        prefs = con.execute(
            "SELECT user_id, preference_key, preference_value, source_ids "
            "FROM user_preference_memory "
            "WHERE origin = 'learned' AND active = 1"
        ).fetchall()
    except sqlite3.Error:  # missing table
        con.close()
        return []
    try:
        columns = {row[1] for row in con.execute(
            "PRAGMA table_info(code_findings)")}
    except sqlite3.Error:
        columns = set()
    has_target_repo = "target_repo" in columns
    fallback_root = os.environ.get("MINI_ORK_ROOT") or os.getcwd()

    out: list[dict] = []
    for pref in prefs:
        ids = _source_ids(pref["source_ids"])
        if not ids:
            continue
        placeholders = ",".join("?" for _ in ids)
        select = "SELECT id, file, snippet, ts"
        if has_target_repo:
            select += ", target_repo"
        try:
            rows = con.execute(
                f"{select} FROM code_findings WHERE id IN ({placeholders})", ids
            ).fetchall()
        except sqlite3.Error:  # missing code_findings table
            continue
        found = {int(r["id"]): dict(r) for r in rows}
        if not found:
            # No source survives: nothing to call "fresh", but also no evidence
            # the rule was ever derived from a real finding. Skip rather than
            # claim all-stale.
            continue
        stale_ids: set[int] = set()
        by_repo: dict[str, list[dict]] = {}
        for fid, row in found.items():
            repo = (row.get("target_repo") if has_target_repo else None) \
                or fallback_root
            by_repo.setdefault(str(repo), []).append(row)
        for repo_root, group in by_repo.items():
            annotated = code_findings.annotate_staleness(
                group, repo_root=repo_root, now=now)
            stale_ids.update(int(r["id"]) for r in annotated if r.get("stale"))
        # A source id that no longer exists cannot be fresh; count it stale.
        # One fresh source is enough to keep the rule.
        if not all(fid in stale_ids or fid not in found for fid in ids):
            continue
        out.append({
            "kind": "rule",
            "preference_key": pref["preference_key"],
            "text": pref["preference_value"],
            "source_ids": ids,
            "state": "active",
            "recommendation": "retire",
            "evidence": f"{len(ids)} of {len(ids)} source findings stale",
        })
    out.sort(key=lambda d: d["preference_key"])
    con.close()
    return out
