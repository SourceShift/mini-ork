"""Board — epics on the left, every run of the project on the right.

Two tabs:

- ``tasks`` (default) — one ``columns`` section of epic cards. The columns
  come from the scheduler's view of epics (``Backlog`` / ``Working`` /
  ``Needs you`` / ``Done``); every card carries the latest ``epic_attempts``
  row's run id when one exists, else the epic's kickoff path resolved
  against ``MINI_ORK_ROOT`` when the file is on disk, else no ``do`` key.
- ``runs`` — the live run board: four columns of fleet cards (Working /
  Needs you / Failed / Done), Done over a rolling 24-hour window.

A header ``New task`` thread action (``/task …``) opens a new mini-ork
thread to author one. The thread action is a thread, not a ``cli`` —
``tests/unit/test_ide_pages_actions.py`` parses every ``cli`` action the
IDE exposes, and ``/task`` is intentionally not a subcommand.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

TABS = [("tasks", "Tasks"), ("runs", "Runs")]

# ── runs tab ────────────────────────────────────────────────────────────────
# Per-column card cap.
_CARD_CAP = 30
# ``fleet_rows`` clamps ``limit`` to its own ``MAX_LIMIT = 50``.
_FLEET_LIMIT = 50
# Done is a rolling window — the board is about what is in flight now.
_DONE_WINDOW = 24 * 3600

# (column title, colour, the fleet state it gathers).
COLUMNS = (
    ("Working", "blue", "working"),
    ("Needs you", "yellow", "needs_you"),
    ("Failed", "red", "failed"),
    ("Done", "green", "done"),
)


def _fleet(home: Path, state: str):
    from mini_ork.acp.fleet import fleet_rows

    return fleet_rows(home, state=state, limit=_FLEET_LIMIT)


def _change(added: int, removed: int) -> str:
    return f"+{added} −{removed}" if (added or removed) else ""


def _card(row: Any) -> dict[str, Any]:
    title = row.title or row.run_id
    sub = f"{row.recipe} · {row.step}" if row.step else str(row.recipe or "")
    meta = [S.meta_item(S.money(row.cost_usd), mono=True)]
    change = _change(row.added, row.removed)
    if change:
        meta.append(S.meta_item(change, mono=True))
    return {"id": row.run_id, "title": title, "sub": sub, "state": row.state,
            "mark": row.mark, "meta": meta, "do": S.open_run(row.run_id, title)}


def _cards(home: Path, state: str, now: int, *, window: int | None = None) -> list[dict[str, Any]]:
    rows, _ = _fleet(home, state)
    if window is not None:
        rows = [r for r in rows
                if (r.ended_at or r.started_at or 0) >= now - window]
    rows = sorted(rows, key=lambda r: r.ended_at or r.started_at or 0, reverse=True)
    return [_card(r) for r in rows[:_CARD_CAP]]


def _board(home: Path, now: int, errors: dict[str, str]) -> list[dict[str, Any]]:
    cols: list[dict[str, Any]] = []
    for title, colour, state in COLUMNS:
        window = _DONE_WINDOW if state == "done" else None
        try:
            cards = _cards(home, state, now, window=window)
        except Exception as exc:  # noqa: BLE001 — one column must not blank the board
            errors[title] = f"{type(exc).__name__}: {exc}"
            cards = []
        cols.append(S.column(title, cards, c=colour))
    return [S.columns("Board", cols, full=True)]


# ── tasks tab ───────────────────────────────────────────────────────────────
#
# The status→column mapping (kickoff deliverable 1). Anything outside the
# epics CHECK domain lands in Needs you under the "never silently dropped"
# rule, the same way ``in review`` does. ``blocked`` with a ``held_reason``
# is a scheduler hold for a human and stays out of Backlog.
_TASK_COLUMNS = (
    ("Backlog", "sub"),
    ("Working", "blue"),
    ("Needs you", "yellow"),
    ("Done", "green"),
)
_TASK_CAP = 30


def _db(home: Path):
    from mini_ork.web.db import db_for

    return db_for(home)


def _has_unresolved_hard_dep(db, epic_id: str) -> bool:
    """True when ``epic_id`` has at least one open hard edge into it."""
    if not db.has_table("epic_dependencies"):
        return False
    rows = db.rows(
        "SELECT 1 FROM epic_dependencies WHERE to_epic_id = ? "
        "AND kind = 'hard' AND resolved_at IS NULL LIMIT 1",
        (epic_id,),
    )
    return bool(rows)


def _epics_table_columns(home: Path) -> set[str]:
    """The columns ``epics`` actually carries, via PRAGMA table_info.

    The optional scheduler columns (``priority`` / ``attempts`` /
    ``held_reason`` / ``last_run_id``) exist only after the idempotent
    migrations in :mod:`mini_ork.scheduler` have run; the same pattern
    :func:`mini_ork.orchestration.epic_graph._not_held` uses to tolerate
    an unmigrated home.
    """
    db = _db(home)
    if not db.has_table("epics"):
        return set()
    return {str(r["name"]) for r in db.rows("PRAGMA table_info(epics)")}


def _task_column_for(status: str, held: bool) -> str:
    """Map one epic's status + held flag to its Tasks column.

    - "not started"       → Backlog (the awaiting-deps case lives in Backlog
      too, with the dependency-blocked hint visible on the card)
    - "in progress"       → Working
    - "in review"         → Needs you (outside the documented mapping; never
      silently dropped)
    - "blocked" w/ held   → Needs you (scheduler is holding it for a human)
    - "blocked" w/o held  → Backlog (waiting on deps the system knows about)
    - "escalated"         → Needs you (deadline reached without a run)
    - "done"              → Done
    Anything else is "unknown / Needs you".
    """
    if status == "done":
        return "Done"
    if status == "in progress":
        return "Working"
    if status == "not started":
        return "Backlog"
    if status == "in review":
        return "Needs you"
    if status == "blocked":
        return "Needs you" if held else "Backlog"
    if status == "escalated":
        return "Needs you"
    # Unknown status (or future statuses the page learns about later) lands
    # in Needs you under the "never silently dropped" rule.
    return "Needs you"


def _task_card_state(status: str) -> str:
    if status == "done":
        return "done"
    if status == "in progress":
        return "working"
    if status in ("in review", "blocked", "escalated"):
        return "needs_you"
    return "pending"


def _task_card_mark(status: str) -> str:
    if status == "done":
        return "✓"
    if status == "in progress":
        return "●"
    if status in ("in review", "blocked", "escalated"):
        return "✋"
    if status == "not started":
        return "○"
    return "•"


def _task_card(epic: dict[str, Any], latest_run_id: str | None,
               kickoff_abs: str | None, *, recency: int = 0) -> dict[str, Any]:
    """One epic card for the Tasks board.

    ``do`` prefers the latest ``epic_attempts`` row's run id (``S.open_run``),
    else the kickoff path resolved against ``MINI_ORK_ROOT`` when the file
    exists on disk (``S.open_path``), else no ``do`` key — a card with no
    target does not have a click handler.
    """
    eid = str(epic.get("id") or "")
    title = str(epic.get("title") or eid)
    status = str(epic.get("status") or "")

    sub_parts: list[str] = []
    if status:
        sub_parts.append(status)
    held = epic.get("held_reason")
    if held:
        sub_parts.append(f"held: {held}")
    elif epic.get("dependency_blocked"):
        sub_parts.append("blocked by deps")
    attempts = epic.get("attempts")
    if attempts not in (None, "", 0):
        sub_parts.append(f"attempt {attempts}")
    recipe = str(epic.get("recipe") or epic.get("lane") or "")
    if recipe:
        sub_parts.append(recipe)
    sub = " · ".join(p for p in sub_parts if p)

    if latest_run_id:
        do: dict[str, Any] | None = S.open_run(latest_run_id, title)
    elif kickoff_abs:
        do = S.open_path(str(kickoff_abs))
    else:
        do = None

    return {"id": eid, "title": title, "sub": sub, "state": _task_card_state(status),
            "mark": _task_card_mark(status), "meta": [], "do": do,
            # Internal — sorted on, stripped before the page serialises.
            "_recency": int(recency)}


def _sort_task_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by priority DESC, then by updated_at DESC (recency tie-break).

    Each card carries an internal ``_recency`` ordinal — the SQL assigned it
    to each row before the cards were built — so the second-level sort is
    "newest first within the same priority tier". The kickoff says the cap
    is 30 per column, so any card past the cap drops here.
    """
    def key(card: dict[str, Any]) -> tuple[int, int, str]:
        sub = str(card.get("sub") or "")
        prio = 0
        for part in sub.split(" · "):
            if part.startswith("prio "):
                try:
                    prio = int(part[5:])
                except ValueError:
                    prio = 0
                break
        return (prio, int(card.get("_recency", 0) or 0), str(card.get("id") or ""))

    return sorted(cards, key=key, reverse=True)


def _resolve_kickoff_path(home: Path, kickoff: str) -> str | None:
    """Find the on-disk kickoff path, or ``None``.

    Resolution order (matches the column and the recipes page):

    1. literal ``kickoff_path`` if absolute and on disk,
    2. ``$MINI_ORK_ROOT/<kickoff_path>`` if set and on disk,
    3. ``<home>/<kickoff_path>`` as a last-ditch fallback.
    """
    if not kickoff:
        return None
    root = os.environ.get("MINI_ORK_ROOT") or str(home.absolute().parent)
    candidates: list[Path] = []
    p = Path(kickoff)
    if p.is_absolute():
        candidates.append(p)
    candidates.append(Path(root) / kickoff)
    candidates.append(home / kickoff)
    for c in candidates:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            continue
    return None


def _tasks_board(home: Path, errors: dict[str, str]) -> list[dict[str, Any]]:
    """One ``columns`` section: Backlog / Working / Needs you / Done."""
    db = _db(home)
    if not db.has_table("epics"):
        # Missing table — the page must still build, with a single muted line
        # in place of the board so the operator knows the create isn't here
        # yet. S.guarded wraps the whole read so a missing table still costs
        # only this one section.
        return [S.lst("Tasks", [S.muted("No epics table — initialise the project "
                                       "(``mini-ork init``) to see the Tasks column here.")],
                       full=True)]

    cols = _epics_table_columns(home)
    select_priority = ", e.priority" if "priority" in cols else ", 0 AS priority"
    select_attempts = ", e.attempts" if "attempts" in cols else ", 0 AS attempts"
    select_held = ", e.held_reason" if "held_reason" in cols else ", NULL AS held_reason"
    select_recipe = ", e.recipe" if "recipe" in cols else ", NULL AS recipe"
    select_lane = ", e.lane" if "lane" in cols else ", NULL AS lane"
    # ORDER BY at the SQL layer keeps the priority+recency sort stable when
    # the bucket is larger than the cap (the in-Python sort is a re-sort to
    # match the SQL order, not a fresh ordering).
    order_sql = (" ORDER BY coalesce(e.priority, 0) DESC, e.updated_at DESC"
                 if "priority" in cols else
                 " ORDER BY e.updated_at DESC")
    rows = db.rows(
        "SELECT e.id, e.title, e.status, e.kickoff_path, e.updated_at"
        f"{select_priority}{select_attempts}{select_held}{select_recipe}{select_lane}"
        " FROM epics e WHERE e.archived_at IS NULL" + order_sql
    )

    # Latest epic_attempts row per epic — the join target for ``do``.
    latest: dict[str, str] = {}
    if db.has_table("epic_attempts"):
        for r in db.rows(
            "SELECT ea.epic_id, ea.run_id FROM epic_attempts ea "
            "JOIN (SELECT epic_id, MAX(attempt) AS mx FROM epic_attempts "
            "GROUP BY epic_id) m ON m.epic_id = ea.epic_id AND m.mx = ea.attempt "
            "WHERE ea.run_id IS NOT NULL"
        ):
            eid = str(r.get("epic_id") or "")
            if eid:
                latest[eid] = str(r.get("run_id") or "")

    buckets: dict[str, list[dict[str, Any]]] = {t: [] for t, _ in _TASK_COLUMNS}
    # Use the SQL order as the recency rank: row 0 is the newest, so a
    # descending sort keeps the most-recent card first within a priority tier.
    rows_list = list(rows)
    total = len(rows_list)
    for idx, r in enumerate(rows_list):
        eid = str(r.get("id") or "")
        status = str(r.get("status") or "")
        held = bool(r.get("held_reason"))
        dep_blocked = _has_unresolved_hard_dep(db, eid) and status == "not started"
        if dep_blocked:
            r = dict(r)
            r["dependency_blocked"] = True
        column_title = _task_column_for(status, held)
        kickoff_abs = _resolve_kickoff_path(home, str(r.get("kickoff_path") or ""))
        recency = total - idx  # higher = more recent
        buckets[column_title].append(_task_card(r, latest.get(eid), kickoff_abs,
                                                recency=recency))

    out_cols: list[dict[str, Any]] = []
    for title, colour in _TASK_COLUMNS:
        cards = _sort_task_cards(buckets[title])[:_TASK_CAP]
        # Strip the internal sort key before serialising.
        for card in cards:
            card.pop("_recency", None)
        out_cols.append(S.column(title, cards, c=colour))

    return [S.columns("Tasks", out_cols, full=True,
                      note="Epics archived at the source level never appear here.")]


# ── page ────────────────────────────────────────────────────────────────────


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in dict(TABS) else TABS[0][0]
    now = int(time.time())
    errors: dict[str, str] = {}
    if tab == "runs":
        sections = S.guarded(errors, "Board", lambda: _board(home, now, errors))
    else:
        sections = S.guarded(errors, "Tasks", lambda: _tasks_board(home, errors))
    return S.page("kanban", "Board",
                  "Epics grouped by what the scheduler is doing with them, "
                  "or every run grouped by the state it is in.",
                  actions=[S.btn("New task", S.thread("/task "), "primary")],
                  tabs=TABS, tab=tab, args=args, sections=sections, errors=errors)