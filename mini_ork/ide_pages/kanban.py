"""Board — every run of the project, grouped by the state it is in.

One ``columns`` section with four columns: Working, Needs you, Failed, Done.
Each column holds at most 30 cards, newest first; Done covers the last 24 h
only (and includes runs delivered via ``landed.json``). Data:
``mini_ork.acp.fleet.fleet_rows``, the same read ``runs.py`` and ``inbox.py``
use, so the board can never disagree with the rest of the IDE.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

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


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    now = int(time.time())
    errors: dict[str, str] = {}
    sections = S.guarded(errors, "Board", lambda: _board(home, now, errors))
    return S.page("kanban", "Board", "Every run, grouped by the state it is in.",
                  sections=sections, errors=errors, args=args)
