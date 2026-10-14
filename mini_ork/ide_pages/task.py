"""Task — one epic, or one mirrored ticket, in full.

A card on the Board opens here (:func:`mini_ork.ide_pages.spec.open_task`); the
IDE gives each ``uid`` its own tab, the way it does for a run. Everything is
read from the home's ``state.db`` and, for an epic, its kickoff file on disk: no
PM adapter is imported and no network call is made — the same rule the Board
follows, so a slow or broken Linear/Jira/GitHub source can neither slow nor
blank this page.

An epic has a body to show: the kickoff file, the attempt history, the
dependency edges in and out, and the open ``inbox`` items waiting for a human. A
ticket does not: the mirror stores no body, no ``created_at`` and no comments
(``mini_ork.pm.store.IssueRow`` says why), so the ticket view draws the columns
the mirror does hold, links to the source, and says plainly that the rest is a
live read it deliberately does not make.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mini_ork.ide_pages import spec as S

# The Board owns both of these: the kickoff-on-disk resolution (three candidate
# roots) and the ISO-stamp → epoch read. Two copies of either is how two pages
# come to disagree about which kickoff a task has, or how old it is.
from mini_ork.ide_pages.kanban import _iso_epoch, _resolve_kickoff_path  # noqa: PLC2701

# The kickoff source, verbatim. A kickoff is an authored markdown file, not a
# log: past this it is a runaway, and the page is one tab, not a viewer.
_KICKOFF_CAP = 200_000

_STATUS_COLOUR = {
    "done": "green",
    "in progress": "blue",
    "in review": "yellow",
    "blocked": "yellow",
    "escalated": "red",
    "not started": "sub",
}

_KIND_COLOUR = {"hard": "red", "soft": "yellow", "informational": "sub"}

# The ``epic_attempts.outcome`` words the scheduler settles an attempt with
# (``scheduler._settle`` and its dispatch), and how the table reads them. An
# unknown word is drawn as itself under the "never silently dropped" rule the
# Board's column mapping follows.
_OUTCOME_COLOUR = {"done": "green", "held": "yellow", "failed": "red", "not_run": "sub"}

# The normalized priority counts up (``Priority.URGENT`` is the highest number),
# exactly as ``epics.priority`` does.
_PRIORITY_LABELS = {0: "no priority", 1: "low", 2: "medium", 3: "high", 4: "urgent"}


def _db(home: Path):
    from mini_ork.web.db import db_for

    return db_for(home)


def _stamp(value: Any, now: int) -> str:
    """An ISO stamp as ``<stamp> · <age> ago``, or the raw value when unparseable.

    The epics table writes ``strftime('%Y-%m-%dT%H:%M:%fZ','now')``, so the raw
    string is already the answer to "which moment"; the age is what makes it
    readable at a glance.
    """
    if not value:
        return ""
    text = str(value)
    epoch = _iso_epoch(text)
    if epoch is None:
        return text
    age = S.age(epoch, now)
    return text if age == "now" else f"{text} · {age} ago"


def _kv_items(pairs: list[tuple]) -> list[tuple]:
    """Drop the pairs whose value is blank — a fact we do not have is not a row.

    The Board's cards do the same with their ``sub`` line: an empty
    ``worker_default`` must not claim a worker called "".
    """
    return [p for p in pairs if p[1] not in (None, "", [])]


def _page(uid: str, title: str, sub: str, sections: list[dict[str, Any]],
          actions: list[dict[str, Any]], errors: dict[str, str],
          args: dict[str, str], *, label: str = "Task") -> dict[str, Any]:
    """The envelope for every exit of :func:`build`.

    ``label`` is what the IDE tab shows — ``page.rs::MiniOrkPage::label`` reads
    it before falling back to the page key — so a task tab reads as the epic it
    holds instead of as a second "Task".
    """
    page = S.page("task", title, sub, actions=actions,
                  args={**dict(args or {}), "uid": uid},
                  sections=sections, errors=errors)
    page["label"] = label
    return page


def build(home: Path, tab: str | None, args: dict[str, str]) -> dict[str, Any]:
    del tab  # one view: the task itself is the tab
    uid = str((args or {}).get("uid") or "").strip()
    errors: dict[str, str] = {}
    back = [S.btn("Board", S.page_link("kanban"), "ghost")]
    if not uid:
        return _page(uid, "Task", "No task selected.",
                     [S.lst("Task", [S.dot("Open a card on the Board to see it here.")],
                            full=True)], back, errors, args)
    if not (home / "state.db").is_file():
        return _page(uid, uid, "This project has no state.db yet.",
                     [S.lst("Task", [S.dot(f"No state.db under {home} — "
                                             "nothing to read a task from.")], full=True)],
                     back, errors, args)

    db = _db(home)
    epic: dict[str, Any] | None = None
    if db.has_table("epics"):
        try:
            epic = db.row("SELECT * FROM epics WHERE id = ?", (uid,))
        except Exception as exc:  # noqa: BLE001 — an unreadable epics table must still page
            errors["Epic"] = f"{type(exc).__name__}: {exc}"
    if epic:
        return _epic_page(home, db, uid, epic, args, errors)
    ticket = _ticket(home, uid, errors)
    if ticket:
        return _ticket_page(home, uid, ticket, args, errors)
    return _page(uid, uid, "Not found in this project.",
                 [S.lst("Task", [S.dot("No epic with that id, and no mirrored ticket "
                                         "with that uid.")], full=True)],
                 back, errors, args)


# ── epic ────────────────────────────────────────────────────────────────────


def _epic_page(home: Path, db, uid: str, epic: dict[str, Any], args: dict[str, str],
               errors: dict[str, str]) -> dict[str, Any]:
    now = int(time.time())
    title = str(epic.get("title") or uid)
    sections: list[dict[str, Any]] = []
    sections.extend(_needs_you(db, uid, epic, now, errors))
    sections.extend(S.guarded(errors, "Epic", lambda: _facts(home, epic, now)))
    sections.extend(_kickoff(home, epic, errors))
    sections.extend(_attempts(db, uid, now, errors))
    sections.extend(_dependencies(db, uid, errors))
    return _page(uid, title, _sub(epic), sections, _actions(home, uid, epic),
                 errors, args, label=title)


def _sub(epic: dict[str, Any]) -> str:
    parts = ["Epic", str(epic.get("status") or "")]
    recipe = str(epic.get("recipe") or epic.get("lane") or "")
    if recipe:
        parts.append(recipe)
    if epic.get("archived_at"):
        parts.append("archived")
    return " · ".join(p for p in parts if p)


def _facts(home: Path, epic: dict[str, Any], now: int) -> dict[str, Any]:
    """The epic's own columns, read from the row the Board drew the card from."""
    status = str(epic.get("status") or "")
    attempts = epic.get("attempts")
    max_attempts = epic.get("max_attempts")
    attempt_text = ""
    if attempts is not None or max_attempts:
        attempt_text = (f"{attempts or 0} of {max_attempts}" if max_attempts
                        else str(attempts or 0))
    kickoff = _resolve_kickoff_path(home, str(epic.get("kickoff_path") or ""))
    items = _kv_items([
        ("Status", status or "—", _STATUS_COLOUR.get(status, "text")),
        ("Recipe", epic.get("recipe") or "", "text", "MO_SCHED_RECIPE when empty"),
        ("Lane", epic.get("lane") or ""),
        ("Worker", epic.get("worker_default") or "", "text", "scheduler default when empty"),
        ("Reviewer", epic.get("reviewer") or ""),
        ("Attempts", attempt_text, "text", "epic_attempts rows so far"),
        ("Priority", epic.get("priority") if epic.get("priority") is not None else "",
         "text", "base; the scheduler inherits the max over blocked waiters"),
        ("Kind", epic.get("epic_kind") or ""),
        ("Group", epic.get("group_id") or ""),
        ("Estimate", f"{epic['estimated_days']} d" if epic.get("estimated_days") else ""),
        ("Created", _stamp(epic.get("created_at"), now)),
        ("Updated", _stamp(epic.get("updated_at"), now)),
        ("Archived", _stamp(epic.get("archived_at"), now)),
        ("Held", epic.get("held_reason") or "", "yellow", "the scheduler is holding this for you"),
        ("Salvage", epic.get("salvage_attempts") or ""),
        ("Last conflict", epic.get("last_conflict_kind") or ""),
        ("Kickoff", kickoff or str(epic.get("kickoff_path") or ""),
         "text" if kickoff else "sub",
         "on disk" if kickoff else "not found under MINI_ORK_ROOT or the home"),
        ("Notes", epic.get("notes") or ""),
    ])
    return S.kv("Epic", items, full=True)


def _kickoff(home: Path, epic: dict[str, Any], errors: dict[str, str]) -> list[dict[str, Any]]:
    def build() -> dict[str, Any]:
        raw = str(epic.get("kickoff_path") or "")
        path = _resolve_kickoff_path(home, raw)
        if not path:
            return S.lst("Kickoff", [S.dot(
                "No kickoff file on disk"
                + (f" — epics.kickoff_path is {raw!r}" if raw else " recorded for this epic"))],
                full=True)
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return S.lst("Kickoff", [S.bad("Could not read the kickoff", f"{path}: {exc}")],
                         full=True)
        if len(text) > _KICKOFF_CAP:
            text = text[:_KICKOFF_CAP] + "\n\n(truncated)\n"
        return S.markdown(Path(path).name, text, path=path, full=True)

    return S.guarded(errors, "Kickoff", build)


def _needs_you(db, uid: str, epic: dict[str, Any], now: int,
               errors: dict[str, str]) -> list[dict[str, Any]]:
    """What put this epic in the Board's "Needs you" column, above everything else.

    A scheduler hold or an escalation is a state, not a message, so it gets a
    callout of its own; the open ``inbox`` rows carry the actual markdown. No
    callout has a resolving button: the ``inbox`` table's ``resolution`` is a
    human decision and nothing but a human writes it (``verify.py`` draws the
    same rows without one). ``source_run_id`` is deliberately not read — it
    points at the legacy integer ``runs.id``, not at a ``run-<epoch>-<n>`` run,
    so opening it as a run would land the reader on someone else's run.
    """
    def build() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        status = str(epic.get("status") or "")
        held = str(epic.get("held_reason") or "")
        if held:
            out.append(S.callout("Held by the scheduler", held, tone="yellow", icon="✋",
                                 full=True))
        elif status == "escalated":
            out.append(S.callout(
                "Escalated", "This epic reached its attempt cap without a passing attempt. "
                             "``epic_attempts`` below has the history; **Retry** puts it back "
                             "in the queue.", tone="red", icon="✗", full=True))
        if not db.has_table("inbox"):
            return out
        rows = db.rows(
            "SELECT id, kind, body_md, opened_at FROM inbox "
            "WHERE epic_id = ? AND resolved_at IS NULL ORDER BY opened_at DESC", (uid,))
        for row in rows:
            epoch = _iso_epoch(row.get("opened_at"))
            kind = str(row.get("kind") or "item")
            raised = f"{kind} · raised {S.age(epoch, now)} ago" if epoch is not None else kind
            out.append(S.callout(f"Needs you · {kind}", str(row.get("body_md") or ""),
                                 tone="yellow", icon="✋", meta=[S.muted(raised)], full=True))
        return out

    return S.guarded(errors, "Needs you", build)


def _attempts(db, uid: str, now: int, errors: dict[str, str]) -> list[dict[str, Any]]:
    def build() -> dict[str, Any]:
        if not db.has_table("epic_attempts"):
            return S.lst("Attempts", [S.dot("No attempt history — the retry schema "
                                              "is created the first time the scheduler runs")])
        rows = db.rows(
            "SELECT attempt, run_id, recipe, verdict, outcome, reason, started_at, finished_at "
            "FROM epic_attempts WHERE epic_id = ? ORDER BY attempt DESC", (uid,))
        if not rows:
            return S.lst("Attempts", [S.dot("No attempts yet")])
        out = []
        for row in rows:
            outcome = str(row.get("outcome") or "")
            run_id = str(row.get("run_id") or "")
            reason = " ".join(str(row.get("reason") or "").split())
            out.append({
                "cells": [S.cell(f"#{row.get('attempt')}", None, mono=True),
                          S.cell(outcome or "running", _OUTCOME_COLOUR.get(outcome, "sub")),
                          S.cell(str(row.get("verdict") or "—")),
                          S.cell(str(row.get("recipe") or "—")),
                          S.mono(run_id or "—"),
                          S.cell(_stamp(row.get("started_at"), now), "sub"),
                          S.cell(reason[:200] or "—", "sub")],
                "do": S.open_run(run_id, f"{uid} · attempt {row.get('attempt')}") if run_id else None,
            })
        return S.table("Attempts",
                       [S.col(40), S.col(90), S.col(90), S.col(130), S.col(210), S.col(230),
                        S.col(None, fr=3)],
                       ["#", "Outcome", "Verdict", "Recipe", "Run", "Started", "Why"], out,
                       full=True)

    return S.guarded(errors, "Attempts", build)


def _dependencies(db, uid: str, errors: dict[str, str]) -> list[dict[str, Any]]:
    """Both directions of ``epic_dependencies``, each row opening the other epic.

    Direction matters and is named: an unresolved *incoming* hard edge is what
    keeps this epic in the Board's Backlog, while an outgoing one is an epic
    this one is holding back.
    """
    def build() -> dict[str, Any]:
        if not db.has_table("epic_dependencies"):
            return S.lst("Dependencies", [S.dot("No epic_dependencies table")])
        rows = db.rows(
            "SELECT from_epic_id, to_epic_id, kind, resolved_at FROM epic_dependencies "
            "WHERE from_epic_id = ? OR to_epic_id = ? ORDER BY kind, from_epic_id, to_epic_id",
            (uid, uid))
        if not rows:
            return S.lst("Dependencies", [S.dot("No dependency edges")])
        out = []
        for row in rows:
            outgoing = str(row.get("from_epic_id")) == uid
            other = str(row.get("to_epic_id") if outgoing else row.get("from_epic_id"))
            kind = str(row.get("kind") or "")
            resolved = row.get("resolved_at") is not None
            out.append({
                "cells": [S.cell("blocks" if outgoing else "blocked by", "sub"),
                          S.cell(other),
                          S.cell(kind, _KIND_COLOUR.get(kind, "text")),
                          S.cell("resolved" if resolved else "unresolved",
                                 "green" if resolved else "yellow")],
                "do": S.open_task(other),
            })
        return S.table("Dependencies", [S.col(100), S.col(None, fr=3), S.col(120), S.col(120)],
                       ["Edge", "Epic", "Kind", "State"], out, full=True)

    return S.guarded(errors, "Dependencies", build)


def _actions(home: Path, uid: str, epic: dict[str, Any]) -> list[dict[str, Any]]:
    """The epic's verbs, in the header.

    ``home=False`` on every one of them: ``board.rs`` appends ``--home <home>``
    at the END of the argv, and both ``epics`` and ``scheduler`` reject an
    unknown trailing flag with rc 2 (``epics._retry``, the scheduler flag loop).
    The action path always exports ``MINI_ORK_HOME``, which is what they read.
    """
    status = str(epic.get("status") or "")
    actions: list[dict[str, Any]] = [
        S.btn("Dispatch now",
              S.cli("scheduler", "--once", "--epic", uid, home=False,
                    confirm=f"Dispatch {uid} to the scheduler now?"), "primary"),
        S.btn("Dry run",
              S.cli("scheduler", "--once", "--epic", uid, "--dry-run", home=False), "ghost"),
    ]
    held = bool(epic.get("held_reason"))
    if status == "escalated" or (status == "blocked" and held):
        actions.append(S.btn("Retry", S.cli("epics", "retry", uid, home=False,
                                            confirm=f"Put {uid} back in the queue?"), "ghost"))
    kickoff = _resolve_kickoff_path(home, str(epic.get("kickoff_path") or ""))
    if kickoff:
        actions.append(S.btn("Kickoff file", S.open_path(kickoff), "ghost"))
    return actions


# ── mirrored ticket ─────────────────────────────────────────────────────────


def _ticket(home: Path, uid: str, errors: dict[str, str]) -> Any:
    """The mirror row for ``uid``, or ``None`` — never a network call.

    ``store.row`` reads the home's own ``state.db`` by explicit path, so a test's
    temp home can never reach a developer's mirror.
    """
    try:
        from mini_ork.pm import store

        return store.row(uid, db=str(home / "state.db"))
    except Exception as exc:  # noqa: BLE001 — no mirror, no ticket view
        errors["Ticket"] = f"{type(exc).__name__}: {exc}"
        return None


def _ticket_page(home: Path, uid: str, row: Any, args: dict[str, str],
                 errors: dict[str, str]) -> dict[str, Any]:
    now = int(time.time())
    title = str(getattr(row, "title", "") or uid)
    provider = str(getattr(row, "provider", "") or "")
    native_key = str(getattr(row, "native_key", "") or "")
    source = f"{provider}:{native_key}" if native_key else provider
    url = str(getattr(row, "url", "") or "")
    sections: list[dict[str, Any]] = [S.callout(
        "Mirrored, not live",
        "This is the PM mirror's row, and the mirror deliberately stores no body, "
        "no `created_at` and no comments — a view that needs them re-reads the issue "
        "through its adapter. The Board makes no network call, so neither does this "
        "page: open the source to see the rest.",
        tone="orange", icon="•", full=True)]
    sections.extend(S.guarded(errors, "Ticket", lambda: _ticket_facts(row, now)))
    sections.extend(S.guarded(errors, "Links", lambda: _ticket_links(home, uid)))
    actions = [S.btn("Board", S.page_link("kanban"), "ghost")]
    if url:
        actions.insert(0, S.btn(f"Open in {provider or 'the source'}", S.url(url), "primary"))
    return _page(uid, title, f"Ticket · {source}" if source else "Ticket",
                 sections, actions, errors, args, label=title)


def _ticket_facts(row: Any, now: int) -> dict[str, Any]:
    priority = getattr(row, "priority", None)
    state = str(getattr(row, "state", "") or "")
    native_state = str(getattr(row, "native_state", "") or "")
    labels = getattr(row, "labels", None)
    if isinstance(labels, (list, tuple)):
        labels = ", ".join(str(x) for x in labels)
    # ``Priority`` is an IntEnum, so this is the normalized ordinal; the
    # source's own number is ``native_priority`` and is deliberately NOT shown
    # — two numberings side by side is how a reader picks the wrong one.
    priority_label = ""
    if priority is not None:
        try:
            priority_label = _PRIORITY_LABELS.get(int(priority), str(priority))
        except (TypeError, ValueError):
            priority_label = str(priority)
    items = _kv_items([
        ("Source", f"{getattr(row, 'provider', '') or ''}:{getattr(row, 'native_key', '') or ''}"),
        ("State", state or "—", "text",
         f"the source calls it {native_state!r}" if native_state and native_state != state else ""),
        ("Priority", priority_label, "text", "normalized"),
        ("Assignee", getattr(row, "assignee", "") or ""),
        ("Labels", labels or ""),
        ("Project", getattr(row, "project", "") or ""),
        ("Parent", getattr(row, "parent_uid", "") or ""),
        ("Updated", _stamp(getattr(row, "updated_at", None), now)),
        ("Synced", _stamp(getattr(row, "synced_at", None), now),
         "text", "when the mirror last read the source"),
        ("URL", getattr(row, "url", "") or ""),
    ])
    return S.kv("Ticket", items, full=True)


def _ticket_links(home: Path, uid: str) -> dict[str, Any]:
    """The mirror's edges for this ticket; a ``run:`` edge opens that run."""
    from mini_ork.pm import store

    rows = store.links_for(uid, db=str(home / "state.db"))
    items = []
    for link in rows:
        other = link.to_uid if link.from_uid == uid else link.from_uid
        kind = str(link.kind)
        if other.startswith("run:"):
            run_id = other[len("run:"):]
            items.append(S.dot(f"run {run_id}", kind,
                               acts=[S.btn("Open", S.open_run(run_id, f"{uid} · {kind}"),
                                           "ghost")]))
        else:
            items.append(S.dot(other, kind, acts=[S.btn("Open", S.open_task(other), "ghost")]))
    return S.lst("Links", items or [S.dot("No links recorded for this ticket")], full=True)
