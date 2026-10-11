"""mini-ork's own epics, seen through the PM model.

This adapter is the reason :class:`~mini_ork.pm.provider.Capabilities` is a
declared value rather than a convention. The ``epics`` table looks writable —
it is one UPDATE away — but a status change belongs to ``scheduler._settle``,
and the trigger ``trg_epics_no_revert_done`` refuses a done epic going back out
of done. So ``transition`` is false here and the board does not offer the drag.
There is no comment thread either, and the only relationship an epic has is
``epic_dependencies``, which expresses *blocks* and nothing else.

Reads are generous: every column the runtime may or may not have added since
``0001_core.sql`` is selected only when ``PRAGMA table_info`` says it is there,
so an old state.db yields a thinner card rather than an error.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, Sequence

from mini_ork.pm.model import (
    Comment,
    Issue,
    IssueState,
    Link,
    LinkKind,
    Priority,
    ProviderIdentity,
    split_uid,
)
from mini_ork.pm.provider import (
    Capabilities,
    Draft,
    Page,
    Patch,
    ProviderError,
    Query,
    Unsupported,
)

#: mini-ork status → the normalized state. ``escalated`` is a blocked epic that
#: needs a person, which is the only thing BLOCKED means here.
STATE_BY_NATIVE: dict[str, IssueState] = {
    "not started": IssueState.TODO,
    "in progress": IssueState.IN_PROGRESS,
    "in review": IssueState.IN_REVIEW,
    "blocked": IssueState.BLOCKED,
    "escalated": IssueState.BLOCKED,
    "done": IssueState.DONE,
}

#: The inverse, derived rather than written twice: two native statuses collapse
#: onto BLOCKED, and a hand-maintained inverse would eventually disagree.
NATIVE_BY_STATE: dict[IssueState, tuple[str, ...]] = {}
for _native, _state in STATE_BY_NATIVE.items():
    NATIVE_BY_STATE[_state] = NATIVE_BY_STATE.get(_state, ()) + (_native,)

#: The values ``epics.epic_kind`` is allowed to hold (0001_core.sql).
EPIC_KINDS = frozenset({"fe", "be", "llm", "data", "sandbox", "doc", "mixed"})

#: Written by ``epics add`` and every other creator, so an issue made here is
#: indistinguishable from one made on the command line.
DEFAULT_STATUS = "not started"

#: The columns a card would like. Whichever of these the database actually has
#: is what gets selected and carried in ``Issue.native``.
OPTIONAL_COLUMNS = (
    "priority",
    "epic_kind",
    "lane",
    "worker_default",
    "reviewer",
    "recipe",
    "group_id",
    "kickoff_path",
    "estimated_days",
    "attempts",
    "max_attempts",
    "last_run_id",
    "held_reason",
    "primary_journey_id",
)

_REQUIRED_COLUMNS = ("id", "title", "status", "notes", "created_at", "updated_at", "archived_at")


def _slugify(title: str) -> str:
    """Same shape as ``cli/epics._slugify``: epic ids are lowercase slugs."""
    slug = re.sub(r"[^a-z0-9-]+", "-", title.lower().strip()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return (slug or "epic")[:60]


def _db_path(db: str | None) -> str:
    if db:
        return db
    env = os.environ.get("MINI_ORK_DB")
    if env:
        return env
    return os.path.join(os.environ.get("MINI_ORK_HOME", ".mini-ork"), "state.db")


def _conn(db: str | None) -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(db))
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    return con


def _columns(con: sqlite3.Connection) -> set[str]:
    return {row[1] for row in con.execute("PRAGMA table_info(epics)").fetchall()}


def _selected(available: set[str]) -> tuple[str, ...]:
    """Everything the card wants, narrowed to what this database has.

    ``id``, ``title`` and ``status`` are the columns an epics table cannot do
    without, so their absence is reported here rather than three frames down a
    traceback.
    """
    missing = {"id", "title", "status"} - available
    if missing:
        raise ProviderError(
            "no usable epics table in this state.db "
            f"(missing {', '.join(sorted(missing))}) — run `mini-ork init` first"
        )
    return tuple(name for name in _REQUIRED_COLUMNS + OPTIONAL_COLUMNS if name in available)


def _newest_first(available: set[str]) -> str:
    """Order by the freshest timestamp the table actually has.

    ``updated_at`` can be NULL on a row nothing has touched since ingest, so it
    is coalesced when both columns exist; on a table that has only one of them,
    or neither, the id keeps the order stable rather than failing the query.
    """
    if {"updated_at", "created_at"} <= available:
        return "ORDER BY COALESCE(updated_at, created_at) DESC, id"
    if "updated_at" in available:
        return "ORDER BY updated_at DESC, id"
    if "created_at" in available:
        return "ORDER BY created_at DESC, id"
    return "ORDER BY id"


def _has(record: sqlite3.Row, name: str) -> bool:
    return name in record.keys()


def _issue(record: sqlite3.Row) -> Issue:
    status = str(record["status"] or "")
    native_priority = record["priority"] if _has(record, "priority") else None
    kind = record["epic_kind"] if _has(record, "epic_kind") else None
    archived = record["archived_at"] if _has(record, "archived_at") else None
    if archived:
        # An archived epic is out of play without being done — the closest
        # thing mini-ork has to a cancelled issue, and keeping it out of every
        # open column is the whole point of saying so.
        state = IssueState.CANCELED
    else:
        state = STATE_BY_NATIVE.get(status, IssueState.BACKLOG)
    return Issue(
        identity=ProviderIdentity(provider="local", key=str(record["id"]), url=None),
        title=str(record["title"] or ""),
        state=state,
        native_state=status,
        description=str(record["notes"] or "") if _has(record, "notes") else "",
        priority=Priority.from_mini_ork(native_priority),
        native_priority=int(native_priority) if native_priority is not None else None,
        labels=tuple(part for part in str(kind or "").split(",") if part),
        # Deliberately not `worker_default`: mini-ork assigns work to a lane
        # template, never to a person, which is why `assignees` is false.
        assignee=None,
        parent_uid=None,
        created_at=record["created_at"] if _has(record, "created_at") else None,
        updated_at=record["updated_at"] if _has(record, "updated_at") else None,
        blocked_reason=record["held_reason"] if _has(record, "held_reason") else None,
        native={name: record[name] for name in OPTIONAL_COLUMNS if _has(record, name)},
    )


def _where(query: Query, columns: set[str]) -> tuple[str, list[Any]]:
    """SQL for a :class:`Query`. Empty filters vanish; unmappable ones are exact.

    ``BACKLOG`` and ``CANCELED`` have no native spelling because mini-ork has
    neither concept: BACKLOG matches nothing at all — an epic exists in order to
    be worked on — and CANCELED is the archive flag rather than a status.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if query.text.strip():
        # title is guaranteed by _selected; notes and id are not, so the clause
        # is built from what the table has rather than from a fixed list.
        searched = [name for name in ("title", "notes", "id") if name in columns]
        clauses.append("(" + " OR ".join(f"{name} LIKE ?" for name in searched) + ")")
        needle = f"%{query.text.strip()}%"
        params.extend([needle] * len(searched))

    if query.states:
        statuses: list[str] = []
        for state in query.states:
            if state is IssueState.CANCELED and "archived_at" in columns:
                clauses.append("archived_at IS NOT NULL")
                continue
            statuses.extend(NATIVE_BY_STATE.get(state, ()))
        if statuses:
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        elif IssueState.CANCELED not in query.states:
            # Only unmappable states were asked for. Nothing matches, and
            # returning the whole table as if the filter had applied is worse
            # than returning nothing.
            clauses.append("0")
    elif "archived_at" in columns:
        clauses.append("archived_at IS NULL")

    if query.priorities and "priority" in columns:
        clauses.append(f"priority IN ({','.join('?' * len(query.priorities))})")
        params.extend(int(priority) for priority in query.priorities)
    if query.labels and "epic_kind" in columns:
        clauses.append("(" + " OR ".join("epic_kind LIKE ?" for _ in query.labels) + ")")
        params.extend(f"%{label}%" for label in query.labels)
    return (" AND ".join(clauses) or "1", params)


def _unique_id(con: sqlite3.Connection, slug: str) -> str:
    """``slug``, ``slug-2``, ``slug-3`` … — the ids `epics ingest` also mints."""
    if not con.execute("SELECT 1 FROM epics WHERE id = ?", (slug,)).fetchone():
        return slug
    suffix = 2
    while con.execute("SELECT 1 FROM epics WHERE id = ?", (f"{slug}-{suffix}",)).fetchone():
        suffix += 1
    return f"{slug}-{suffix}"


class LocalProvider:
    """The epics table as a PM source. No network, no credential, always on."""

    id = "local"
    label = "mini-ork epics"
    capabilities = Capabilities(
        create=True,
        update=True,
        # `scheduler._settle` owns status, and trg_epics_no_revert_done blocks
        # the reverse of done.
        transition=False,
        comment=False,
        link=True,
        search=True,
        labels=False,
        assignees=False,
        pagination=False,
    )

    def __init__(self, db: str | None = None) -> None:
        self._db = db

    # ── reads ──────────────────────────────────────────────────────────────

    def list_issues(self, query: Query) -> Page:
        con = _conn(self._db)
        try:
            columns = _columns(con)
            selected = ", ".join(_selected(columns))
            where, params = _where(query, columns)
            limit = max(1, int(query.limit))
            records = con.execute(
                f"SELECT {selected} FROM epics WHERE {where} "
                f"{_newest_first(columns)} LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        finally:
            con.close()
        has_more = len(records) > limit
        issues = tuple(_issue(record) for record in records[:limit])
        return Page(issues=issues, has_more=has_more, cursor=None, total=None if has_more else len(issues))

    def get(self, uid: str) -> Issue:
        provider_id, key = split_uid(uid)
        if provider_id != self.id:
            raise KeyError(f"{uid} is not a {self.id} issue")
        con = _conn(self._db)
        try:
            columns = _columns(con)
            selected = ", ".join(_selected(columns))
            record = con.execute(f"SELECT {selected} FROM epics WHERE id = ?", (key,)).fetchone()
        finally:
            con.close()
        if record is None:
            raise KeyError(f"no epic {key!r}")
        return _issue(record)

    def comments(self, uid: str) -> Sequence[Comment]:
        raise Unsupported(self.id, "comments")

    # ── writes ─────────────────────────────────────────────────────────────

    def create(self, draft: Draft) -> Issue:
        """Map a draft onto an epic row.

        ``Draft.state`` is a request, not a promise: mini-ork opens everything
        at ``not started``, and a state it cannot spell (BACKLOG, CANCELED) is
        that same default, because there is no backlog column to put it in.
        """
        title = draft.title.strip()
        if not title:
            raise ProviderError("an epic needs a title")
        status = NATIVE_BY_STATE.get(draft.state, (DEFAULT_STATUS,))[0]
        for label in draft.labels:
            if label not in EPIC_KINDS:
                raise ProviderError(
                    f"{label!r} is not an epic kind; one of: {', '.join(sorted(EPIC_KINDS))}"
                )
        if draft.assignee or draft.parent_uid:
            raise Unsupported(self.id, "assignee or parent on an epic")

        # Priority is a runtime add, so a state.db old enough not to have the
        # column must still accept a created epic.
        from mini_ork.scheduler import ensure_priority_column

        ensure_priority_column(self._db)
        con = _conn(self._db)
        try:
            columns = _columns(con)
            epic_id = _unique_id(con, _slugify(title))
            names = ["id", "title", "status"]
            values: list[Any] = [epic_id, title[:200], status]
            if draft.description and "notes" in columns:
                names.append("notes")
                values.append(draft.description)
            if "priority" in columns:
                names.append("priority")
                values.append(int(draft.priority))
            if draft.labels and "epic_kind" in columns:
                names.append("epic_kind")
                values.append(draft.labels[0])
            con.execute(
                f"INSERT INTO epics({', '.join(names)}) VALUES ({','.join('?' * len(names))})",
                values,
            )
            con.commit()
            selected = ", ".join(_selected(columns))
            record = con.execute(f"SELECT {selected} FROM epics WHERE id = ?", (epic_id,)).fetchone()
        finally:
            con.close()
        return _issue(record)

    def update(self, uid: str, patch: Patch) -> Issue:
        """Title, description and priority. Status is not among them — it is
        :meth:`transition`, and this source does not support that."""
        provider_id, key = split_uid(uid)
        if provider_id != self.id:
            raise KeyError(f"{uid} is not a {self.id} issue")
        if patch.assignee is not None or patch.parent_uid is not None or patch.labels is not None:
            # Refused rather than dropped: a caller that asked for one of these
            # believes the write happened.
            raise Unsupported(self.id, "assignee, labels or parent on an epic")

        assignments: list[str] = []
        values: list[Any] = []
        if patch.title is not None:
            assignments.append("title = ?")
            values.append(patch.title.strip()[:200])
        if patch.description is not None:
            assignments.append("notes = ?")
            values.append(patch.description)
        if patch.priority is not None:
            from mini_ork.scheduler import ensure_priority_column

            ensure_priority_column(self._db)
            assignments.append("priority = ?")
            values.append(int(patch.priority))
        if not assignments:
            return self.get(uid)

        con = _conn(self._db)
        try:
            columns = _columns(con)
            values.append(key)
            cursor = con.execute(f"UPDATE epics SET {', '.join(assignments)} WHERE id = ?", values)
            if cursor.rowcount == 0:
                raise KeyError(f"no epic {key!r}")
            con.commit()
            selected = ", ".join(_selected(columns))
            record = con.execute(f"SELECT {selected} FROM epics WHERE id = ?", (key,)).fetchone()
        finally:
            con.close()
        return _issue(record)

    def transition(self, uid: str, state: IssueState) -> Issue:
        raise Unsupported(self.id, "transition (status belongs to the scheduler)")

    def comment(self, uid: str, body: str) -> Comment:
        raise Unsupported(self.id, "comment")

    def link(self, from_uid: str, to_uid: str, kind: LinkKind) -> Link:
        """``blocks`` and ``relates`` only.

        ``epic_dependencies`` calls those hard and soft. Anything else — a
        duplicate, a parent, a run edge — has no column to live in, so it is
        refused rather than written down as a soft dependency and remembered
        wrongly.
        """
        from mini_ork.orchestration import epic_graph

        # Resolve first: an enum member hashes by its NAME, so a plain
        # "blocks" string would miss the dict below rather than hit it.
        resolved = LinkKind(kind)
        native_kinds = {LinkKind.BLOCKS: "hard", LinkKind.RELATES: "soft"}
        if resolved not in native_kinds:
            raise Unsupported(self.id, f"{resolved} links")
        from_provider, from_key = split_uid(from_uid)
        to_provider, to_key = split_uid(to_uid)
        if from_provider != self.id or to_provider != self.id:
            raise Unsupported(self.id, "links across sources")
        epic_graph.add_dep(from_key, to_key, native_kinds[resolved], self._db)
        return Link(from_uid=from_uid, to_uid=to_uid, kind=resolved)


def provider(db: str | None = None) -> LocalProvider:
    """One instance, for a caller that has no config to consult."""
    return LocalProvider(db)
