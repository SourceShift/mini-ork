"""The local mirror of every source's issues.

The mirror is an *index*, not a second source of truth. It exists so the board
and the search box can answer without a network round-trip, and so a link
between a mini-ork run and a Linear issue survives that issue being edited,
renamed or moved on the remote side — none of which mini-ork controls. The row
it holds is deliberately partial, which is why :class:`IssueRow` is its own
type and not an :class:`~mini_ork.pm.model.Issue`: a caller that needs a
description has to go back to the adapter that owns it, and cannot be fooled
into thinking the mirror is complete.

Two writers, one schema. :func:`ensure_schema` runs the same DDL the migration
``0069_pm_mirror.sql`` does, because a state.db that predates the migration and
a state.db that never ran one both have to work — the scheduler's
``ensure_retry_schema`` sets that precedent. A test asserts the two texts agree,
so the pair cannot drift apart quietly.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Sequence

from mini_ork.pm.model import Issue, IssueState, Link, LinkKind, Priority
from mini_ork.pm.provider import Query

#: Every statement :func:`ensure_schema` runs, in order. ``0069_pm_mirror.sql``
#: carries the same text; ``tests/unit/test_pm_store.py`` holds them together.
SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS pm_issues (
      uid             TEXT PRIMARY KEY,
      provider        TEXT NOT NULL,
      native_key      TEXT NOT NULL,
      title           TEXT NOT NULL,
      state           TEXT NOT NULL,
      native_state    TEXT NOT NULL,
      priority        INTEGER NOT NULL DEFAULT 0,
      native_priority INTEGER,
      url             TEXT,
      assignee        TEXT,
      labels          TEXT,
      parent_uid      TEXT,
      project         TEXT,
      updated_at      TEXT,
      synced_at       TEXT NOT NULL,
      UNIQUE (provider, native_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pm_issues_provider_state ON pm_issues(provider, state)",
    "CREATE INDEX IF NOT EXISTS idx_pm_issues_updated ON pm_issues(updated_at)",
    """
    CREATE TABLE IF NOT EXISTS pm_links (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      from_uid   TEXT NOT NULL,
      to_uid     TEXT NOT NULL,
      kind       TEXT NOT NULL
                 CHECK (kind IN ('blocks','relates','duplicates','parent','run')),
      created_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_pm_links_edge ON pm_links(from_uid, to_uid, kind)",
    "CREATE INDEX IF NOT EXISTS idx_pm_links_from ON pm_links(from_uid)",
    "CREATE INDEX IF NOT EXISTS idx_pm_links_to ON pm_links(to_uid)",
    """
    CREATE TABLE IF NOT EXISTS pm_sync (
      provider  TEXT PRIMARY KEY,
      synced_at TEXT NOT NULL,
      n         INTEGER NOT NULL,
      error     TEXT
    )
    """,
)


def _db_path(db: str | None) -> str:
    """Explicit path → ``MINI_ORK_DB`` → ``$MINI_ORK_HOME/state.db``.

    Same resolution as ``orchestration.epic_graph`` and the rest of the
    control plane; duplicated rather than shared because every module in this
    codebase owns its own connection policy.
    """
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
    con.row_factory = sqlite3.Row
    return con


@dataclass(frozen=True, slots=True)
class IssueRow:
    """One mirror row: what the index knows, and nothing it does not.

    There is no ``description`` and no ``native`` payload here — those live
    behind the adapter. A view that needs them re-reads the issue by uid.
    """

    uid: str
    provider: str
    native_key: str
    title: str
    state: IssueState
    native_state: str
    priority: Priority
    native_priority: int | None
    url: str | None
    assignee: str | None
    labels: tuple[str, ...]
    parent_uid: str | None
    project: str | None
    updated_at: str | None
    synced_at: str


def _row_from(record: sqlite3.Row) -> IssueRow:
    return IssueRow(
        uid=record["uid"],
        provider=record["provider"],
        native_key=record["native_key"],
        title=record["title"],
        # A state written by a newer version of this package than the reader:
        # read it as BACKLOG rather than failing the whole board over one row.
        state=_state(record["state"]),
        native_state=record["native_state"],
        priority=_priority(record["priority"]),
        native_priority=record["native_priority"],
        url=record["url"],
        assignee=record["assignee"],
        labels=tuple(json.loads(record["labels"] or "[]")),
        parent_uid=record["parent_uid"],
        project=record["project"],
        updated_at=record["updated_at"],
        synced_at=record["synced_at"],
    )


def _state(value: str) -> IssueState:
    try:
        return IssueState(value)
    except ValueError:
        return IssueState.BACKLOG


def _priority(value: int | None) -> Priority:
    try:
        return Priority(int(value or 0))
    except ValueError:
        return Priority.NONE


def ensure_schema(db: str | None = None) -> None:
    """Idempotent: safe on every open, and on a database that never migrated."""
    con = _conn(db)
    try:
        for statement in SCHEMA_SQL:
            con.execute(statement)
        con.commit()
    finally:
        con.close()


def upsert_issues(
    provider_id: str,
    issues: Iterable[Issue],
    db: str | None = None,
    *,
    synced_at: str | None = None,
) -> int:
    """Write (or refresh) one batch of issues. Returns the number of rows written.

    ``synced_at`` is passed in rather than read from the clock here so a caller
    mirroring several batches stamps them all with one instant — otherwise a
    page showing "synced 3 seconds ago" next to a row from an hour ago reads as
    a bug in the clock.

    An issue whose identity names a different source than ``provider_id`` is
    refused: the row would carry one provider in its uid and another in its
    column, and every later query would disagree with itself.
    """
    stamp = synced_at or _now()
    con = _conn(db)
    try:
        for statement in SCHEMA_SQL:
            con.execute(statement)
        written = 0
        for issue in issues:
            if issue.identity.provider != provider_id:
                raise ValueError(
                    f"issue {issue.uid!r} belongs to {issue.identity.provider!r}, "
                    f"not {provider_id!r}"
                )
            con.execute(
                """
                INSERT INTO pm_issues (uid, provider, native_key, title, state,
                                       native_state, priority, native_priority, url,
                                       assignee, labels, parent_uid, project,
                                       updated_at, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(uid) DO UPDATE SET
                  title=excluded.title,
                  state=excluded.state,
                  native_state=excluded.native_state,
                  priority=excluded.priority,
                  native_priority=excluded.native_priority,
                  url=excluded.url,
                  assignee=excluded.assignee,
                  labels=excluded.labels,
                  parent_uid=excluded.parent_uid,
                  project=excluded.project,
                  updated_at=excluded.updated_at,
                  synced_at=excluded.synced_at
                """,
                (
                    issue.uid,
                    provider_id,
                    issue.identity.key,
                    issue.title,
                    str(issue.state),
                    issue.native_state,
                    int(issue.priority),
                    issue.native_priority,
                    issue.identity.url,
                    issue.assignee,
                    json.dumps(list(issue.labels)),
                    issue.parent_uid,
                    issue.identity.project,
                    issue.updated_at,
                    stamp,
                ),
            )
            written += 1
        con.execute(
            """
            INSERT INTO pm_sync (provider, synced_at, n, error) VALUES (?,?,?,NULL)
            ON CONFLICT(provider) DO UPDATE SET
              synced_at=excluded.synced_at, n=excluded.n, error=NULL
            """,
            (provider_id, stamp, written),
        )
        con.commit()
        return written
    finally:
        con.close()


def record_sync_error(provider_id: str, error: str, db: str | None = None) -> None:
    """Remember that a source failed, without losing the rows already mirrored.

    The mirror is a cache: a source that is unreachable leaves the last known
    board on screen and notes why it might be stale.
    """
    con = _conn(db)
    try:
        ensure_schema(db)
        con.execute(
            """
            INSERT INTO pm_sync (provider, synced_at, n, error) VALUES (?,?,0,?)
            ON CONFLICT(provider) DO UPDATE SET error=excluded.error
            """,
            (provider_id, _now(), error),
        )
        con.commit()
    finally:
        con.close()


def sync(provider, query: Query | None = None, db: str | None = None) -> int:
    """Pull one page from a source into the mirror. Returns rows written.

    Fails loudly, and records the failure for the UI, rather than returning 0 —
    a board that cannot tell "nothing matched" from "the token expired" will
    show an empty column and let a person believe it.
    """
    from mini_ork.pm.provider import ProviderError

    try:
        page = provider.list_issues(query or Query(limit=200))
    except ProviderError as exc:
        record_sync_error(provider.id, str(exc), db)
        raise
    return upsert_issues(provider.id, page.issues, db)


def query_rows(
    provider: str | None = None,
    states: Sequence[IssueState] = (),
    text: str = "",
    limit: int = 200,
    db: str | None = None,
) -> list[IssueRow]:
    """Mirror rows, newest update first. Filters are ANDed; empty means all."""
    clauses: list[str] = []
    params: list[object] = []
    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if states:
        clauses.append(f"state IN ({','.join('?' * len(states))})")
        params.extend(str(state) for state in states)
    if text.strip():
        # LIKE, not the runs FTS index: this table is small and a substring hit
        # on a key ("ENG-12") is exactly what a person types.
        clauses.append("(title LIKE ? OR native_key LIKE ?)")
        needle = f"%{text.strip()}%"
        params.extend((needle, needle))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, int(limit)))
    con = _conn(db)
    try:
        ensure_schema(db)
        records = con.execute(
            "SELECT * FROM pm_issues" + where +
            " ORDER BY COALESCE(updated_at, synced_at) DESC, uid LIMIT ?",
            params,
        ).fetchall()
        return [_row_from(record) for record in records]
    finally:
        con.close()


def row(uid: str, db: str | None = None) -> IssueRow | None:
    con = _conn(db)
    try:
        ensure_schema(db)
        record = con.execute("SELECT * FROM pm_issues WHERE uid = ?", (uid,)).fetchone()
        return _row_from(record) if record is not None else None
    finally:
        con.close()


def mirror_state(provider_id: str | None = None, db: str | None = None) -> list[dict]:
    """When each source last synced, and how it went — for a staleness line."""
    con = _conn(db)
    try:
        ensure_schema(db)
        params: tuple[object, ...] = ()
        where = ""
        if provider_id:
            where = " WHERE provider = ?"
            params = (provider_id,)
        records = con.execute(
            "SELECT provider, synced_at, n, error FROM pm_sync" + where + " ORDER BY provider",
            params,
        ).fetchall()
        return [dict(record) for record in records]
    finally:
        con.close()


def link(
    from_uid: str,
    to_uid: str,
    kind: LinkKind | str = LinkKind.RELATES,
    db: str | None = None,
) -> Link:
    """Record an edge. Idempotent: the same edge twice is one edge."""
    resolved = LinkKind(kind)
    created = _now()
    con = _conn(db)
    try:
        ensure_schema(db)
        con.execute(
            "INSERT OR IGNORE INTO pm_links (from_uid, to_uid, kind, created_at) VALUES (?,?,?,?)",
            (from_uid, to_uid, str(resolved), created),
        )
        con.commit()
        stored = con.execute(
            "SELECT created_at FROM pm_links WHERE from_uid=? AND to_uid=? AND kind=?",
            (from_uid, to_uid, str(resolved)),
        ).fetchone()
        return Link(
            from_uid=from_uid,
            to_uid=to_uid,
            kind=resolved,
            created_at=stored["created_at"] if stored else created,
        )
    finally:
        con.close()


def links_for(uid: str, db: str | None = None) -> list[Link]:
    """Every edge touching ``uid``, in either direction."""
    con = _conn(db)
    try:
        ensure_schema(db)
        records = con.execute(
            "SELECT from_uid, to_uid, kind, created_at FROM pm_links "
            "WHERE from_uid = ? OR to_uid = ? ORDER BY created_at, id",
            (uid, uid),
        ).fetchall()
        return [
            Link(
                from_uid=record["from_uid"],
                to_uid=record["to_uid"],
                kind=LinkKind(record["kind"]),
                created_at=record["created_at"],
            )
            for record in records
        ]
    finally:
        con.close()


def _now() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
