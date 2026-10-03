"""Read-model queries for the ACP run history surface (Zed Z1+Z2).

Pure read-only functions over a ``.mini-ork`` home's ``state.db``. They feed
``session/list`` and ``session/load`` in ``mini_ork.acp.agent``: listing past
runs and replaying a finished (or live) run's read-model state.

``list_runs`` / ``kickoff_text`` read the ``task_runs`` row plus the kickoff
file staged by ``control.launch_run`` at ``<home>/runs-inbox/<run_id>.md``;
``read_snapshot`` was moved here verbatim from ``MiniOrkAcpAgent._read_snapshot``
so the agent method is a one-line call and the projection stays testable without
a live agent.

Every function guards a missing ``state.db`` (and a missing ``task_runs`` table)
so a bogus or empty home degrades to ``([], None)`` / ``""`` instead of raising —
the same shape the observability UI's ``has_table`` guards produce elsewhere.
``deps`` / ``repositories`` are imported at function level so importing this
module does not pull the FastAPI route graph.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_ork.web.control import _is_safe_token


def _normalize_ts(value: Any) -> str | None:
    """Normalize an epoch-seconds int or an ISO string to an ISO-8601 UTC string.

    ``task_runs.created_at`` / ``updated_at`` are INTEGER unix timestamps
    (migration 0013); defensive callers may hand us an already-ISO string.
    Returns ``None`` for ``None`` and the original text when it is not a
    parseable timestamp.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return text


def _read_kickoff(home: Path, run_id: str, kickoff_path: str | None) -> str:
    """The kickoff markdown for ``run_id``, or "" when it cannot be read.

    Reads the ``kickoff_path`` column first (the authoritative path written by
    ``launch_run``); falls back to ``<home>/runs-inbox/<run_id>.md``. The
    fallback join is gated by ``_is_safe_token`` so a ``..`` in the id cannot
    traverse out of the inbox.
    """
    if kickoff_path:
        try:
            return Path(kickoff_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    if _is_safe_token(run_id):
        fallback = Path(home) / "runs-inbox" / f"{run_id}.md"
        try:
            return fallback.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    return ""


def _title_from_kickoff(text: str) -> str:
    """First non-empty line, stripped of leading ``#``, capped at 80 chars."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped.lstrip("#").strip()[:80]
    return ""


def read_snapshot(home: Path, run_id: str) -> dict[str, Any]:
    """task_run status + node lifecycle events + llm_calls for ``run_id``.

    Moved verbatim from ``MiniOrkAcpAgent._read_snapshot``: reads the task_runs
    row, then bridges node lifecycle events and llm_calls (by trace_id and by
    time window, deduped by row id) through ``RunDetailRepository``. Same return
    shape ``{"status", "events", "llm_calls"}``; a missing DB or missing row
    yields ``{"status": None, "events": [], "llm_calls": []}``.
    """
    from mini_ork.web.deps import db_for
    from mini_ork.web.repositories import RunDetailRepository

    home = Path(home)
    if not (home / "state.db").exists():
        return {"status": None, "events": [], "llm_calls": []}
    repo = RunDetailRepository(db_for(home))
    tr = repo.fetch_task_run_row(run_id)
    if not tr:
        return {"status": None, "events": [], "llm_calls": []}
    events = repo.fetch_node_lifecycle_events(run_id)
    llm_calls: list[dict[str, Any]] = []
    window = repo.fetch_trace_window(run_id)
    if window and window.get("trace_id"):
        llm_calls = repo.fetch_llm_calls_by_trace_id(window["trace_id"])
    if window and window.get("created_at"):
        upper = window.get("ended_at") or int(time.time())
        llm_calls.extend(
            repo.fetch_llm_calls_in_window(int(window["created_at"]), int(upper))
        )
    # The trace_id and time-window bridges can overlap; dedupe by row id.
    seen: set[Any] = set()
    deduped: list[dict[str, Any]] = []
    for row in llm_calls:
        rid = row.get("id")
        if rid in seen:
            continue
        seen.add(rid)
        deduped.append(row)
    return {"status": tr.get("status"), "events": events, "llm_calls": deduped}


def list_runs(
    home: Path, *, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int | None]:
    """Run rows newest-first, paged; ``(rows, next_offset)`` or ``([], None)``.

    Each row maps to ``{"run_id", "status", "recipe", "cost_usd", "created_at",
    "updated_at", "title"}``. ``title`` is the first non-empty kickoff line
    (``#`` stripped, 80-char cap) with a ``"{recipe or 'mini-ork'} run"``
    fallback. Rows whose id is not a safe token are skipped. ``next_offset`` is
    the offset for the next page, or ``None`` when exhausted. A missing DB or
    missing ``task_runs`` table returns ``([], None)``.
    """
    from mini_ork.web.deps import db_for

    home = Path(home)
    if not (home / "state.db").exists():
        return [], None
    try:
        db = db_for(home)
    except (FileNotFoundError, sqlite3.OperationalError):
        return [], None
    try:
        if not db.has_table("task_runs"):
            return [], None
        # Fetch one extra row to decide whether a next page exists; the rowid
        # DESC tiebreaker keeps LIMIT/OFFSET stable when many runs share the
        # same created_at second.
        rows = db.rows(
            """
            SELECT id, recipe, status, cost_usd, created_at, updated_at, kickoff_path
            FROM task_runs
            ORDER BY created_at DESC, rowid DESC
            LIMIT ? OFFSET ?
            """,
            (limit + 1, offset),
        )
    except sqlite3.OperationalError:
        return [], None
    has_more = len(rows) > limit
    rows = rows[:limit]
    out: list[dict[str, Any]] = []
    for row in rows:
        run_id = row.get("id")
        if not isinstance(run_id, str) or not _is_safe_token(run_id):
            continue
        recipe = row.get("recipe")
        kickoff = _read_kickoff(home, run_id, row.get("kickoff_path"))
        title = _title_from_kickoff(kickoff) or f"{recipe or 'mini-ork'} run"
        out.append(
            {
                "run_id": run_id,
                "status": row.get("status"),
                "recipe": recipe,
                "cost_usd": row.get("cost_usd"),
                "created_at": _normalize_ts(row.get("created_at")),
                "updated_at": _normalize_ts(row.get("updated_at")),
                "title": title,
            }
        )
    return out, (offset + limit if has_more else None)


def kickoff_text(home: Path, run_id: str, max_chars: int = 4000) -> str:
    """The kickoff markdown for ``run_id``, truncated to ``max_chars``; "" when absent."""
    home = Path(home)
    kickoff_path: str | None = None
    if (home / "state.db").exists():
        from mini_ork.web.deps import db_for
        from mini_ork.web.repositories import RunDetailRepository

        try:
            repo = RunDetailRepository(db_for(home))
            paths = repo.fetch_input_paths(run_id)
            kickoff_path = paths.get("kickoff_path") if paths else None
        except (FileNotFoundError, sqlite3.OperationalError):
            kickoff_path = None
    return _read_kickoff(home, run_id, kickoff_path)[:max_chars]
