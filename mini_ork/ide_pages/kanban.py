"""Board — epics and imported tickets on the left, every run on the right.

Two tabs:

- ``tasks`` (default) — one ``columns`` section of epic cards *and* the
  tickets imported from the PM mirror (Linear, Jira, GitHub). The columns
  come from the scheduler's view of epics (``Backlog`` / ``Working`` /
  ``Needs you`` / ``Done``); every card carries the latest ``epic_attempts``
  row's run id when one exists, else the epic's kickoff path resolved
  against ``MINI_ORK_ROOT`` when the file is on disk, else no ``do`` key.
  Mirror tickets are read through :mod:`mini_ork.pm.store` only — the page
  makes no network call and imports no adapter, so a slow or broken source
  can neither slow nor blank the board, and a mirror row whose ``provider``
  is ``local`` is skipped (it is already drawn from the ``epics`` table).
- ``runs`` — the live run board: four columns of fleet cards (Working /
  Needs you / Failed / Done), Done over a rolling 24-hour window.

The two delivery tiers (see ``CLAUDE.md``) sit *upstream* of this page and it
behaves the same under either: the simple tier keeps the mirror fresh with
``mini-ork intake poll`` over stdlib HTTP and the key in ``secrets.local.sh``;
the secure tier keeps it fresh with the signed intake webhook and keys in
sops/OpenBao. The board only ever reads the mirror, so which tier filled it is
not its business.

A header ``New task`` thread action (``/task …``) opens a new mini-ork
thread to author one. The thread action is a thread, not a ``cli`` —
``tests/unit/test_ide_pages_actions.py`` parses every ``cli`` action the
IDE exposes, and ``/task`` is intentionally not a subcommand. A ``Sync
tickets`` header button runs ``mini-ork intake once --dry-run``: it refreshes
the mirror and starts nothing.
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

# The closed ``IssueState`` → (Tasks column, card state word, card mark). Every
# state maps; ``canceled`` is deliberately absent from the draw path (it is
# counted in the section note, never drawn). An unmapped word is "never
# silently dropped", the same rule the epic mapping follows.
_TICKET_STATE = {
    "backlog": ("Backlog", "pending", "○"),
    "todo": ("Backlog", "pending", "○"),
    "in_progress": ("Working", "working", "●"),
    "blocked": ("Needs you", "needs_you", "✋"),
    "in_review": ("Needs you", "needs_you", "✋"),
    "done": ("Done", "done", "✓"),
}

# The normalized priority, spelled for a card's meta row. ``Priority`` counts
# up (``URGENT`` is the highest number), exactly as ``epics.priority`` does.
_PRIORITY_LABELS = {0: "no priority", 1: "low", 2: "medium", 3: "high", 4: "urgent"}


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
            "_recency": int(recency),
            "_priority": int(epic.get("priority") or 0)}


def _sort_task_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by priority DESC, then by recency DESC, then id — one order for all cards.

    Each card carries two internal ordinals set at build time: ``_priority``
    (the row's own priority — ``epics.priority`` for an epic, the mirror
    ``Priority`` for a ticket, both counting up) and ``_recency`` (the row's
    rank in its source's newest-first read). Priority leads, so a flood of
    low-priority tickets cannot push a prioritised epic off the board; the
    recency ordinal then keeps the newest card first within a tier. The cap is
    applied on this one order, so epics and tickets share it.
    """
    def key(card: dict[str, Any]) -> tuple[int, int, str]:
        return (int(card.get("_priority", 0) or 0),
                int(card.get("_recency", 0) or 0),
                str(card.get("id") or ""))

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


def _iso_epoch(value: Any) -> int | None:
    """An ISO-8601 sync stamp → a unix epoch, or ``None`` when unparseable."""
    if not value:
        return None
    import datetime as dt

    text = str(value).strip().replace("Z", "+00:00")
    try:
        return int(dt.datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def _ticket_column(state: str) -> tuple[str, str, str]:
    """``IssueState`` → (Tasks column, card state word, card mark).

    An unmapped state (a word this build does not know) is never silently
    dropped: it lands in Needs you, the same rule the epic mapping follows.
    """
    return _TICKET_STATE.get(state, ("Needs you", "needs_you", "✋"))


def _run_id_from_links(uid: str, links) -> str | None:
    """The run id in a ``run`` link touching ``uid``, or ``None``.

    ``links_for`` returns edges in both directions and the run endpoint is
    spelt ``run:<run_id>`` (``mini_ork/threads/service.py``), so take the
    endpoint that is not ``uid`` and strip the prefix; an edge whose far end is
    another issue (``blocks`` / ``relates`` / ``parent``) is not a run link.
    """
    for link in links:
        if str(link.kind) != "run":
            continue
        other = link.to_uid if link.from_uid == uid else link.from_uid
        if other.startswith("run:"):
            return other[len("run:"):]
    return None


def _ticket_card(row: Any, run_id: str | None, *, recency: int = 0) -> dict[str, Any]:
    """One mirror-ticket card for the Tasks board.

    ``title`` leads with the source's own key (``ENG-123``) so a person can
    quote it; the ``sub`` line carries the provider and the source's *native*
    state word, never only the normalized one. ``do`` opens the linked run when
    the ticket has one, else the ticket's own URL, else there is no ``do`` key.
    A run-linked ticket also gets an ``agent`` chip, so a person can see
    mini-ork is working it.
    """
    provider = str(row.provider)
    priority = int(row.priority)
    meta = [S.meta_item(_PRIORITY_LABELS.get(priority, str(priority)), mono=True)]
    if row.assignee:
        meta.append(S.meta_item(str(row.assignee)))
    meta.append(S.meta_item(provider, "cyan"))          # the source chip
    if run_id:
        meta.append(S.meta_item("agent", "green"))      # mini-ork is on it
    if run_id:
        do: dict[str, Any] | None = S.open_run(run_id, str(row.title))
    elif row.url:
        do = S.url(str(row.url))
    else:
        do = None
    _column, state, mark = _ticket_column(str(row.state))
    return {"id": row.uid, "title": f"{row.native_key} · {row.title}",
            "sub": f"{provider} · {row.native_state}", "state": state, "mark": mark,
            "meta": meta, "do": do,
            # Internal — sorted on, stripped before the page serialises.
            "_priority": priority, "_recency": int(recency)}


def _freshness_items(home: Path, now: int) -> list[dict[str, Any]]:
    """The list lines above the columns: the empty state, sync errors, or ages.

    Never a blank board. With no external source enabled the one line names
    ``config/pm.yaml`` and says what a source needs; a source whose last sync
    recorded an ``error`` gets a warn line with the source id and that error
    text — rendered verbatim, because it is already credential-scrubbed at
    write time by ``mini_ork/pm/http.py`` and must never be re-derived from a
    token here; a healthy source shows when it last synced.
    """
    from mini_ork.pm import store
    from mini_ork.pm.config import load_config

    try:
        enabled = [s for s in load_config().enabled() if s.id != "local"]
    except Exception:  # noqa: BLE001 — a broken config must not blank the board
        return []
    if not enabled:
        return [S.muted("No PM source is enabled — add a source to config/pm.yaml "
                        "with `enabled: true` and its `token_env` key in the vault "
                        "(`mini-ork vault status`).")]
    items: list[dict[str, Any]] = []
    mirrored = [s for s in store.mirror_state(db=str(home / "state.db"))
                if s.get("provider") != "local"]
    errored = [s for s in mirrored if s.get("error")]
    if errored:
        for s in errored:
            items.append(S.warn(str(s.get("provider")), str(s.get("error"))))
        return items
    for s in mirrored:
        stamp = str(s.get("synced_at") or "")
        epoch = _iso_epoch(stamp)
        age = S.age(epoch, now) if epoch is not None else stamp
        items.append(S.muted(f"{s.get('provider')} · synced {age} ago"))
    return items


def _source_actions(home: Path) -> list[dict[str, Any]]:
    """Header buttons that set ``args['source']``: All / Epics / one per source.

    ``all`` draws epics and every external ticket; ``local`` is epics only; a
    provider id is that source's tickets only. The provider list is read from
    the mirror, guarded so a missing table costs the buttons, never the page.
    """
    providers: list[str] = []
    try:
        from mini_ork.pm import store

        providers = sorted({
            str(s.get("provider"))
            for s in store.mirror_state(db=str(home / "state.db"))
            if s.get("provider") and s.get("provider") != "local"
        })
    except Exception:  # noqa: BLE001 — no mirror, no per-source buttons
        providers = []
    buttons = [S.btn("All", S.set_args(source="all")),
               S.btn("Epics", S.set_args(source="local"))]
    buttons.extend(S.btn(p, S.set_args(source=p)) for p in providers)
    return buttons


def _epic_buckets(home: Path, db, buckets: dict[str, list[dict[str, Any]]]) -> None:
    """Fill ``buckets`` with the epic cards (today's read, unchanged)."""
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


def _tickets_into(home: Path, buckets: dict[str, list[dict[str, Any]]],
                  source: str) -> int:
    """Fill ``buckets`` with the mirror-ticket cards; return the canceled count.

    Reads only the mirror (never a network call, never a mirror write), skips
    ``local`` rows — those epics are already drawn from the ``epics`` table —
    and applies ``source``: ``all`` keeps every provider, anything else keeps
    just that one. ``canceled`` is not drawn; the count comes back so the
    section note can report it. Every read goes through the home's ``state.db``
    by explicit path, so a test's temp home never touches the developer's
    mirror.
    """
    from mini_ork.pm import store

    mirror_db = str(home / "state.db")
    rows = [r for r in store.query_rows(db=mirror_db) if str(r.provider) != "local"]
    if source not in ("all", "local"):
        rows = [r for r in rows if str(r.provider) == source]
    canceled = 0
    total = len(rows)
    for idx, r in enumerate(rows):
        if str(r.state) == "canceled":
            canceled += 1
            continue
        try:
            links = store.links_for(r.uid, db=mirror_db)
        except Exception:  # noqa: BLE001 — one bad edge must not blank a card
            links = []
        run_id = _run_id_from_links(r.uid, links)
        column_title = _ticket_column(str(r.state))[0]
        buckets[column_title].append(_ticket_card(r, run_id, recency=total - idx))
    return canceled


def _tasks_board(home: Path, args: dict[str, str], now: int,
                 errors: dict[str, str]) -> list[dict[str, Any]]:
    """The Tasks sections: a freshness list, then one ``columns`` section.

    ``args['source']`` filters what is drawn — ``all`` (epics + every external
    ticket), ``local`` (epics only), or a provider id (that source's tickets
    only). Epics and tickets share one order and one per-column cap.
    """
    source = str((args or {}).get("source") or "all").strip() or "all"
    show_epics = source in ("all", "local")
    db = _db(home)
    if show_epics and not db.has_table("epics"):
        # Missing table — the page must still build, with a single muted line
        # in place of the board so the operator knows the create isn't here
        # yet. S.guarded wraps the whole read so a missing table still costs
        # only this one section.
        return [S.lst("Tasks", [S.muted("No epics table — initialise the project "
                                       "(``mini-ork init``) to see the Tasks column here.")],
                       full=True)]

    buckets: dict[str, list[dict[str, Any]]] = {t: [] for t, _ in _TASK_COLUMNS}
    if show_epics:
        _epic_buckets(home, db, buckets)

    canceled = 0
    if source != "local":
        try:
            canceled = _tickets_into(home, buckets, source)
        except Exception as exc:  # noqa: BLE001 — one broken mirror must not blank the board
            errors["Tickets"] = f"{type(exc).__name__}: {exc}"

    out_cols: list[dict[str, Any]] = []
    for title, colour in _TASK_COLUMNS:
        cards = _sort_task_cards(buckets[title])[:_TASK_CAP]
        # Strip the internal sort keys before serialising.
        for card in cards:
            card.pop("_recency", None)
            card.pop("_priority", None)
        out_cols.append(S.column(title, cards, c=colour))

    note = "Epics archived at the source level never appear here."
    if canceled:
        note += (f" {canceled} canceled ticket(s) from the sources are not shown.")

    sections: list[dict[str, Any]] = []
    try:
        items = _freshness_items(home, now)
    except Exception as exc:  # noqa: BLE001 — freshness must not blank the board
        errors["Sources"] = f"{type(exc).__name__}: {exc}"
        items = []
    if items:
        sections.append(S.lst("Sources", items, full=True))
    sections.append(S.columns("Tasks", out_cols, full=True, note=note))
    return sections


# ── page ────────────────────────────────────────────────────────────────────


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    tab = tab if tab in dict(TABS) else TABS[0][0]
    now = int(time.time())
    errors: dict[str, str] = {}
    actions = [S.btn("New task", S.thread("/task "), "primary")]
    if tab == "runs":
        sections = S.guarded(errors, "Board", lambda: _board(home, now, errors))
    else:
        # The mirror controls ride the Tasks tab only: ``Sync tickets`` (an
        # ``intake once --dry-run`` — refreshes the mirror, starts nothing)
        # and the ``source`` filter buttons.
        actions.append(S.btn("Sync tickets", S.cli("intake", "once", "--dry-run")))
        actions.extend(_source_actions(home))
        sections = S.guarded(errors, "Tasks",
                             lambda: _tasks_board(home, args, now, errors))
    return S.page("kanban", "Board",
                  "Epics grouped by what the scheduler is doing with them, "
                  "or every run grouped by the state it is in.",
                  actions=actions,
                  tabs=TABS, tab=tab, args=args, sections=sections, errors=errors)
