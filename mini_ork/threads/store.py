"""Persistence for threads: the transcript, the runs, and the intake ledger.

Owns its own connection policy, exactly as ``mini_ork.pm.store`` does — every
module in this codebase opens its own ``sqlite3`` handle rather than sharing the
read-only pool in ``web.db`` (which is query-only and cannot write a thread).

Two writers, one schema. :func:`ensure_schema` runs the same DDL the migration
``0070_threads.sql`` does, because a state.db that predates the migration and a
state.db that never ran one both have to work — the precedent
``mini_ork.pm.store`` and ``scheduler.ensure_retry_schema`` set. A test asserts
the two texts agree, so the pair cannot drift apart quietly.

The one operation that needs a lock is turn allocation. Two follow-ups arriving
at once — a Slack reply and a websocket tick — must not pick the same turn, so
:func:`append_message` takes ``BEGIN IMMEDIATE`` before reading ``last_turn``.
Everything else is a single statement and needs nothing more than the
``busy_timeout`` the connection already carries.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Sequence

from mini_ork.threads.model import (
    IntakeEvent,
    MessageRole,
    RunStatus,
    Thread,
    ThreadMessage,
    ThreadOrigin,
    ThreadRun,
    ThreadState,
)

#: Every statement :func:`ensure_schema` runs, in order. ``0070_threads.sql``
#: carries the same text; ``tests/unit/test_threads_store.py`` holds them
#: together.
SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS threads (
      id          TEXT PRIMARY KEY,
      title       TEXT NOT NULL DEFAULT '',
      status      TEXT NOT NULL DEFAULT 'idle',
      origin      TEXT NOT NULL DEFAULT 'ide',
      origin_ref  TEXT,
      recipe      TEXT,
      last_turn   INTEGER NOT NULL DEFAULT 0,
      created_at  TEXT NOT NULL,
      updated_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_threads_origin_ref ON threads(origin, origin_ref)",
    "CREATE INDEX IF NOT EXISTS idx_threads_updated ON threads(updated_at)",
    """
    CREATE TABLE IF NOT EXISTS thread_messages (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      thread_id   TEXT NOT NULL,
      turn        INTEGER NOT NULL,
      role        TEXT NOT NULL,
      text        TEXT NOT NULL DEFAULT '',
      event_id    TEXT,
      external_id TEXT,
      attachments TEXT,
      created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_thread_messages_thread "
    "ON thread_messages(thread_id, turn, id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_messages_event "
    "ON thread_messages(thread_id, event_id) WHERE event_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_messages_external "
    "ON thread_messages(thread_id, external_id) WHERE external_id IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS thread_runs (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      thread_id     TEXT NOT NULL,
      run_id        TEXT NOT NULL,
      attempt       INTEGER NOT NULL,
      status        TEXT NOT NULL,
      recipe        TEXT,
      kickoff_path  TEXT,
      started_at    TEXT NOT NULL,
      ended_at      TEXT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_runs_attempt ON thread_runs(thread_id, attempt)",
    "CREATE INDEX IF NOT EXISTS idx_thread_runs_run ON thread_runs(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_thread_runs_live "
    "ON thread_runs(thread_id) WHERE status = 'running'",
    """
    CREATE TABLE IF NOT EXISTS intake_events (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      provider     TEXT NOT NULL,
      delivery_id  TEXT NOT NULL,
      payload_hash TEXT NOT NULL,
      issue_uid    TEXT,
      thread_id    TEXT,
      action       TEXT,
      detail       TEXT,
      created_at   TEXT NOT NULL,
      UNIQUE (provider, delivery_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_intake_events_thread ON intake_events(thread_id)",
)


def _db_path(db: str | None) -> str:
    """Explicit path → ``MINI_ORK_DB`` → ``$MINI_ORK_HOME/state.db``.

    Same resolution as ``pm.store`` and the rest of the control plane;
    duplicated rather than shared because every module owns its own connection
    policy.
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


@contextmanager
def _write(db: str | None) -> Iterator[sqlite3.Connection]:
    """A write transaction: the DDL first (idempotent), then the caller's work.

    ``BEGIN IMMEDIATE`` takes the write lock up front so a read-modify-write
    inside the block — the turn allocator above all — is serialised against a
    second writer instead of racing it to the same value.
    """
    con = _conn(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA_SQL:
            con.execute(statement)
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def ensure_schema(db: str | None = None) -> None:
    """Idempotent: safe on every open, and on a database that never migrated."""
    con = _conn(db)
    try:
        for statement in SCHEMA_SQL:
            con.execute(statement)
        con.commit()
    finally:
        con.close()


def _thread_from(record: sqlite3.Row) -> Thread:
    return Thread(
        id=record["id"],
        title=record["title"],
        status=_thread_state(record["status"]),
        origin=_origin(record["origin"]),
        origin_ref=record["origin_ref"],
        recipe=record["recipe"],
        last_turn=int(record["last_turn"]),
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


def _message_from(record: sqlite3.Row) -> ThreadMessage:
    return ThreadMessage(
        id=int(record["id"]),
        thread_id=record["thread_id"],
        turn=int(record["turn"]),
        role=_role(record["role"]),
        text=record["text"],
        event_id=record["event_id"],
        external_id=record["external_id"],
        attachments=tuple(json.loads(record["attachments"] or "[]")),
        created_at=record["created_at"],
    )


def _run_from(record: sqlite3.Row) -> ThreadRun:
    return ThreadRun(
        id=int(record["id"]),
        thread_id=record["thread_id"],
        run_id=record["run_id"],
        attempt=int(record["attempt"]),
        status=_run_status(record["status"]),
        recipe=record["recipe"],
        kickoff_path=record["kickoff_path"],
        started_at=record["started_at"],
        ended_at=record["ended_at"],
    )


def _intake_from(record: sqlite3.Row) -> IntakeEvent:
    return IntakeEvent(
        id=int(record["id"]),
        provider=record["provider"],
        delivery_id=record["delivery_id"],
        payload_hash=record["payload_hash"],
        issue_uid=record["issue_uid"],
        thread_id=record["thread_id"],
        action=record["action"],
        detail=record["detail"],
        created_at=record["created_at"],
    )


def _thread_state(value: str) -> ThreadState:
    try:
        return ThreadState(value)
    except ValueError:
        # A status written by a newer version than the reader: read it as IDLE
        # rather than failing a whole sidebar over one row.
        return ThreadState.IDLE


def _origin(value: str) -> ThreadOrigin:
    try:
        return ThreadOrigin(value)
    except ValueError:
        return ThreadOrigin.IDE


def _run_status(value: str) -> RunStatus:
    try:
        return RunStatus(value)
    except ValueError:
        return RunStatus.ERROR


def _role(value: str) -> MessageRole:
    try:
        return MessageRole(value)
    except ValueError:
        return MessageRole.SYSTEM


def open_thread(
    thread_id: str,
    *,
    title: str = "",
    origin: ThreadOrigin | str = ThreadOrigin.IDE,
    origin_ref: str | None = None,
    recipe: str | None = None,
    db: str | None = None,
) -> Thread:
    """Open (or return) a thread. Idempotent: a second call never resets it.

    The initial run id is the thread id, so an ``ide`` conversation that mints
    its own uuid and an intake thread that derives ``github-owner-name`` both
    hand ``open_thread`` a key the rest of the system already understands.
    """
    now = _now()
    with _write(db) as con:
        con.execute(
            "INSERT OR IGNORE INTO threads "
            "(id, title, status, origin, origin_ref, recipe, last_turn, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,0,?,?)",
            (thread_id, title, str(ThreadState.IDLE), str(origin), origin_ref, recipe, now, now),
        )
        record = con.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return _thread_from(record)


def get_thread(thread_id: str, db: str | None = None) -> Thread | None:
    con = _conn(db)
    try:
        ensure_schema(db)
        record = con.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return _thread_from(record) if record is not None else None
    finally:
        con.close()


def find_thread(
    origin: ThreadOrigin | str, origin_ref: str, db: str | None = None
) -> Thread | None:
    """The newest thread opened for a provider key, or ``None``.

    Newest, not *the*: ``(origin, origin_ref)`` is not unique on purpose — a
    ticket may legitimately open a second thread — so a caller asking "which
    thread owns this ticket?" gets the most recent one and decides for itself,
    typically by checking whether that thread has a live run.
    """
    con = _conn(db)
    try:
        ensure_schema(db)
        record = con.execute(
            "SELECT * FROM threads WHERE origin = ? AND origin_ref = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (str(origin), origin_ref),
        ).fetchone()
        return _thread_from(record) if record is not None else None
    finally:
        con.close()


def list_threads(limit: int = 100, db: str | None = None) -> list[Thread]:
    """Threads, most recently touched first."""
    con = _conn(db)
    try:
        ensure_schema(db)
        records = con.execute(
            "SELECT * FROM threads ORDER BY updated_at DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [_thread_from(record) for record in records]
    finally:
        con.close()


def append_message(
    thread_id: str,
    *,
    role: MessageRole | str,
    text: str = "",
    turn: int | None = None,
    event_id: str | None = None,
    external_id: str | None = None,
    attachments: Sequence[str] = (),
    db: str | None = None,
) -> ThreadMessage:
    """Append one message, and return the row — the new one, or the existing
    one when ``event_id`` or ``external_id`` was already stored.

    ``turn=None`` opens a new turn (allocated under ``BEGIN IMMEDIATE`` — the
    message that starts an attempt); an explicit ``turn`` attaches to an
    existing round (a run's projected output). A duplicate ``event_id`` or
    ``external_id`` is a no-op returning the stored row, which is what makes
    ``observe`` idempotent on every poll tick and a Slack redelivery harmless.
    """
    now = _now()
    with _write(db) as con:
        existing = _existing_message(con, thread_id, event_id, external_id)
        if existing is not None:
            return _message_from(existing)

        if turn is None:
            record = con.execute(
                "SELECT last_turn FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if record is None:
                raise KeyError(f"no such thread: {thread_id!r} (open_thread first)")
            turn = int(record["last_turn"]) + 1
            con.execute(
                "UPDATE threads SET last_turn = ?, updated_at = ? WHERE id = ?",
                (turn, now, thread_id),
            )
        else:
            # An explicit turn must still advance the allocator, or the next
            # `turn=None` re-allocates a turn that already has rows: the mirror
            # writes attempt 1's turn 1 explicitly, and the follow-up would then
            # be handed turn 1 again — putting two rounds under one number and
            # naming the continuation's run after the wrong turn. `max` because
            # a projected run output can legitimately attach to an earlier turn.
            con.execute(
                "UPDATE threads SET last_turn = MAX(last_turn, ?), updated_at = ? "
                "WHERE id = ?",
                (int(turn), now, thread_id),
            )

        try:
            cursor = con.execute(
                "INSERT INTO thread_messages "
                "(thread_id, turn, role, text, event_id, external_id, attachments, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (thread_id, int(turn), str(role), text, event_id, external_id,
                 json.dumps(list(attachments)), now),
            )
        except sqlite3.IntegrityError:
            # A concurrent writer stored the same event_id/external_id between
            # our check and our insert. Re-read rather than surface a raw
            # SQLite error: the row the caller wanted exists.
            stored = _existing_message(con, thread_id, event_id, external_id)
            if stored is None:
                raise
            return _message_from(stored)
        record = con.execute(
            "SELECT * FROM thread_messages WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _message_from(record)


def _existing_message(
    con: sqlite3.Connection, thread_id: str, event_id: str | None, external_id: str | None
) -> sqlite3.Row | None:
    if event_id is not None:
        row = con.execute(
            "SELECT * FROM thread_messages WHERE thread_id = ? AND event_id = ?",
            (thread_id, event_id),
        ).fetchone()
        if row is not None:
            return row
    if external_id is not None:
        return con.execute(
            "SELECT * FROM thread_messages WHERE thread_id = ? AND external_id = ?",
            (thread_id, external_id),
        ).fetchone()
    return None


def messages(
    thread_id: str, limit: int | None = None, db: str | None = None
) -> list[ThreadMessage]:
    """The transcript in order. ``limit`` keeps the LAST N rows, not the first."""
    con = _conn(db)
    try:
        ensure_schema(db)
        if limit is None:
            records = con.execute(
                "SELECT * FROM thread_messages WHERE thread_id = ? ORDER BY turn, id",
                (thread_id,),
            ).fetchall()
        else:
            records = con.execute(
                "SELECT * FROM thread_messages WHERE thread_id = ? "
                "ORDER BY turn DESC, id DESC LIMIT ?",
                (thread_id, max(1, int(limit))),
            ).fetchall()
            records = list(reversed(records))
        return [_message_from(record) for record in records]
    finally:
        con.close()


def message_by_external(
    thread_id: str, external_id: str, db: str | None = None
) -> ThreadMessage | None:
    """The stored row for a remote message id, or ``None`` — the redelivery probe."""
    con = _conn(db)
    try:
        ensure_schema(db)
        record = con.execute(
            "SELECT * FROM thread_messages WHERE thread_id = ? AND external_id = ?",
            (thread_id, external_id),
        ).fetchone()
        return _message_from(record) if record is not None else None
    finally:
        con.close()


def link_run(
    thread_id: str,
    run_id: str,
    *,
    attempt: int,
    recipe: str | None = None,
    kickoff_path: str | None = None,
    db: str | None = None,
) -> ThreadRun:
    """Record a launch and mark the thread running. Idempotent per attempt.

    ``INSERT OR IGNORE`` on the unique ``(thread_id, attempt)`` index means a
    second caller trying to dispatch the same attempt is a no-op returning the
    existing row — the double-dispatch guard, enforced by the schema rather than
    by a caller remembering to check.
    """
    now = _now()
    with _write(db) as con:
        con.execute(
            "INSERT OR IGNORE INTO thread_runs "
            "(thread_id, run_id, attempt, status, recipe, kickoff_path, started_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (thread_id, run_id, int(attempt), str(RunStatus.RUNNING), recipe, kickoff_path, now),
        )
        con.execute(
            "UPDATE threads SET status = ?, updated_at = ? WHERE id = ?",
            (str(ThreadState.RUNNING), now, thread_id),
        )
        record = con.execute(
            "SELECT * FROM thread_runs WHERE thread_id = ? AND attempt = ?",
            (thread_id, int(attempt)),
        ).fetchone()
        return _run_from(record)


def live_run(thread_id: str, db: str | None = None) -> ThreadRun | None:
    """The in-flight run for a thread, or ``None``.

    One index seek on the partial ``idx_thread_runs_live`` index, which holds
    only the runs that are still going.
    """
    con = _conn(db)
    try:
        ensure_schema(db)
        record = con.execute(
            "SELECT * FROM thread_runs WHERE thread_id = ? AND status = 'running' "
            "ORDER BY attempt DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return _run_from(record) if record is not None else None
    finally:
        con.close()


def live_run_for_issue(issue_uid: str, db: str | None = None) -> ThreadRun | None:
    """The run currently working an issue, found through ``pm_links``.

    The ledger answers "has a run ever worked this ticket"; only this answers
    "is one working it *now*", which is the question the poller asks before it
    starts a second run.

    It is a join because ``pm_links`` holds edges and no status: the run's
    association with the issue is ``kind='run'`` with ``to_uid`` spelt
    ``run:<run id>`` (see :func:`mini_ork.threads.service._link_issue`), and
    liveness lives in ``thread_runs`` alone. The ``running`` predicate is served
    by the partial ``idx_thread_runs_live`` index, so this stays one seek.

    ``None`` covers three states a caller treats the same — no link, no run
    row, and no ``pm_links`` table at all, which is what a home that has never
    synced a PM source looks like.
    """
    con = _conn(db)
    try:
        ensure_schema(db)
        record = con.execute(
            "SELECT r.* FROM pm_links AS l "
            "JOIN thread_runs AS r ON r.run_id = substr(l.to_uid, 5) "
            "WHERE l.from_uid = ? AND l.kind = 'run' AND r.status = 'running' "
            "ORDER BY r.attempt DESC LIMIT 1",
            (issue_uid,),
        ).fetchone()
        return _run_from(record) if record is not None else None
    except sqlite3.Error:
        # `pm_links` belongs to the PM mirror, which has its own schema and its
        # own migration. A home that has never synced one has no such table,
        # and "no run is working this" is the honest reading of that.
        return None
    finally:
        con.close()


def runs_for(thread_id: str, db: str | None = None) -> list[ThreadRun]:
    """Every launch in a thread, oldest attempt first."""
    con = _conn(db)
    try:
        ensure_schema(db)
        records = con.execute(
            "SELECT * FROM thread_runs WHERE thread_id = ? ORDER BY attempt",
            (thread_id,),
        ).fetchall()
        return [_run_from(record) for record in records]
    finally:
        con.close()


def settle_run(
    run_id: str, status: RunStatus | str, db: str | None = None
) -> Thread | None:
    """Mark a run terminal and re-derive its thread's status. Returns the thread.

    Keyed by ``run_id`` — that is what the run-completion side knows — and a
    no-op when the run is already terminal, so a second completion signal cannot
    flip a finished thread back to running.
    """
    resolved = RunStatus(status)
    now = _now()
    con = _conn(db)
    try:
        ensure_schema(db)
        record = con.execute(
            "SELECT thread_id FROM thread_runs WHERE run_id = ? AND status = 'running'",
            (run_id,),
        ).fetchone()
        if record is None:
            return None
        thread_id = record["thread_id"]
        con.execute(
            "UPDATE thread_runs SET status = ?, ended_at = ? WHERE run_id = ? AND status = 'running'",
            (str(resolved), now, run_id),
        )
        con.commit()
    finally:
        con.close()
    return recompute_status(thread_id, db)


def recompute_status(thread_id: str, db: str | None = None) -> Thread | None:
    """Rebuild ``threads.status`` from ``thread_runs`` — the cache's repairer.

    Running beats everything; otherwise the newest attempt's terminal value
    wins; a thread with no runs is idle.
    """
    con = _conn(db)
    try:
        ensure_schema(db)
        if con.execute(
            "SELECT 1 FROM thread_runs WHERE thread_id = ? AND status = 'running' LIMIT 1",
            (thread_id,),
        ).fetchone() is not None:
            status = ThreadState.RUNNING
        else:
            record = con.execute(
                "SELECT status FROM thread_runs WHERE thread_id = ? ORDER BY attempt DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            if record is None:
                status = ThreadState.IDLE
            elif record["status"] == "error":
                status = ThreadState.ERROR
            else:
                status = ThreadState.FINISHED
        con.execute(
            "UPDATE threads SET status = ?, updated_at = ? WHERE id = ?",
            (str(status), _now(), thread_id),
        )
        con.commit()
        stored = con.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return _thread_from(stored) if stored is not None else None
    finally:
        con.close()


def record_intake_event(
    provider: str,
    delivery_id: str,
    payload_hash: str,
    *,
    issue_uid: str | None = None,
    thread_id: str | None = None,
    action: str | None = None,
    detail: str | None = None,
    db: str | None = None,
) -> IntakeEvent:
    """Record what an intake trigger did with one event. Idempotent per delivery.

    A replayed webhook or a re-poll carries the same ``(provider, delivery_id)``;
    the unique index folds that to one row and the stored ``action`` is returned
    unchanged, so the caller can see it already happened rather than acting twice.
    """
    now = _now()
    with _write(db) as con:
        con.execute(
            "INSERT OR IGNORE INTO intake_events "
            "(provider, delivery_id, payload_hash, issue_uid, thread_id, action, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (provider, delivery_id, payload_hash, issue_uid, thread_id, action, detail, now),
        )
        record = con.execute(
            "SELECT * FROM intake_events WHERE provider = ? AND delivery_id = ?",
            (provider, delivery_id),
        ).fetchone()
        return _intake_from(record)


def intake_event(
    provider: str, delivery_id: str, db: str | None = None
) -> IntakeEvent | None:
    """A previously recorded delivery, or ``None`` — the replay probe."""
    con = _conn(db)
    try:
        ensure_schema(db)
        record = con.execute(
            "SELECT * FROM intake_events WHERE provider = ? AND delivery_id = ?",
            (provider, delivery_id),
        ).fetchone()
        return _intake_from(record) if record is not None else None
    finally:
        con.close()


def intake_events_for(
    issue_uid: str, limit: int = 50, db: str | None = None
) -> list[IntakeEvent]:
    """Every event recorded against one issue uid, newest first.

    The "has anything happened for this ticket?" history a board shows beside a
    ticket, and the input the dedup logic reads.
    """
    con = _conn(db)
    try:
        ensure_schema(db)
        records = con.execute(
            "SELECT * FROM intake_events WHERE issue_uid = ? ORDER BY id DESC LIMIT ?",
            (issue_uid, max(1, int(limit))),
        ).fetchall()
        return [_intake_from(record) for record in records]
    finally:
        con.close()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
