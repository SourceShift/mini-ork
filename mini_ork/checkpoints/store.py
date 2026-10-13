"""SQL primitives for the ``node_completion_checkpoints`` table.

This file is the thin DB layer behind ``mini_ork.checkpoints``. It does
NOT enforce the module-level ON/OFF gate — that lives in ``__init__.py``
so a recipe that flips the gate off does not have to load SQLite at all.

Why a thin layer at all (versus putting it all in ``__init__.py``)? The
table write is a single ``INSERT OR REPLACE`` with a fixed column order;
splitting keeps the resume filter (``filter_resume_set``) readable without
``sqlite3`` boilerplate getting in the way, and gives the tests a
narrow surface to monkey-patch if they ever need to. Mirrors the layout
of ``mini_ork/stores/checkpoints.py`` (writer / reader / dataclass split)
which the durable-DAG uses for the same reason.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional


_TABLE = "node_completion_checkpoints"


_INSERT = (
    "INSERT OR REPLACE INTO {t} ("
    " run_id, step_id, node_id, status, artifact_path, artifact_sha256,"
    " rc, started_at, ended_at"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
).format(t=_TABLE)


_SELECT_FOR_RUN = (
    "SELECT run_id, step_id, node_id, status, artifact_path, artifact_sha256,"
    " rc, started_at, ended_at, recorded_at"
    " FROM {t} WHERE run_id = ? ORDER BY id ASC"
).format(t=_TABLE)


_SELECT_FOR_STEP = (
    "SELECT run_id, step_id, node_id, status, artifact_path, artifact_sha256,"
    " rc, started_at, ended_at, recorded_at"
    " FROM {t} WHERE run_id = ? AND step_id = ?"
).format(t=_TABLE)


# Terminal-status vocabulary per F1 + F2 / F3 / F4. Mirrored here so the
# SQL layer can validate the column at the boundary (the SQL CHECK is the
# last line of defence; the in-process F1 is the first).
_VALID_STATUSES = frozenset({"succeeded", "failed", "error"})


def shape_reason(
    *,
    status: str | None,
    artifact_path: str | None,
    artifact_sha256: str | None,
    started_at: int | None,
    ended_at: int | None,
) -> str | None:
    """Return why a completion row violates the F1 shape, or ``None`` if it
    is well-formed.

    The write path (:func:`record_completion_db`) and the read path
    (:meth:`mini_ork.checkpoints.CompletionRecord.from_row`) share this one
    predicate on purpose. The SQL CHECK enforces ``status`` at the table
    boundary, but artifact-pairing and timestamp NOT-NULLs are only implied
    by the writer's F1 contract — so a row that predates the CHECK, survives
    a schema change, or is hand-edited could read back as valid. F1 says the
    shape IS the contract; a reader that defaults a malformed row to "looks
    fine" is exactly the silent re-execution the checkpointer exists to
    prevent. Validating both ends against one predicate means a row the
    writer would have refused can never be read as if it had been accepted.
    """
    if status not in _VALID_STATUSES:
        return f"status {status!r} is not one of {sorted(_VALID_STATUSES)}"
    if artifact_path is None and artifact_sha256 is not None:
        return "artifact_sha256 is set but artifact_path is missing"
    if artifact_path is not None and artifact_sha256 is None:
        return "artifact_path is set but artifact_sha256 is missing"
    if started_at is None or ended_at is None:
        return "started_at and ended_at must both be set"
    return None


@dataclass(frozen=True)
class CompletionRow:
    """Internal dataclass for a ``node_completion_checkpoints`` row.

    F1 fields (per the SpecCard):
        run_id, step_id, node_id, status, artifact_path, artifact_sha256,
        rc, started_at, ended_at
    Plus ``recorded_at`` (set by the SQL default; not part of F1 but
    needed for "which record is the latest one?" when a step has more
    than one completion row — the UNIQUE constraint prevents that, but
    ``recorded_at`` is still useful for ``list_completions`` ordering
    diagnostics when a recipe chooses to log the full history).
    """

    run_id: str
    step_id: str
    node_id: str
    status: str
    artifact_path: Optional[str]
    artifact_sha256: Optional[str]
    rc: Optional[int]
    started_at: int
    ended_at: int
    recorded_at: int


def _open(db: str) -> sqlite3.Connection:
    """Open ``db`` with the same pragmas ``init_db`` applies (WAL, sync).

    Test fixtures pass ``:memory:`` or a tmp path; both work. The pragmas
    here are safe on both and harmless on a DB that has already been
    initialised (WAL is idempotent; synchronous=NORMAL persists across
    connections in SQLite).
    """
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con


def record_completion_db(
    db: str,
    *,
    run_id: str,
    step_id: str,
    node_id: str,
    status: str,
    artifact_path: Optional[str] = None,
    artifact_sha256: Optional[str] = None,
    rc: Optional[int] = None,
    started_at: int,
    ended_at: int,
) -> int:
    """INSERT OR REPLACE one completion record in one transaction.

    Returns 0 on success. Returns 2 on SQL failure (the gate / module is
    open and the writer still failed; the dispatch loop must NOT crash on
    this — durable-DAG ledger is the source of truth for reuse). Returns 3
    on input-shape failure (F1 violation caught before SQL, so the
    caller gets a typed diagnostic instead of a generic sqlite3.Error).
    """
    if shape_reason(
        status=status,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
        started_at=started_at,
        ended_at=ended_at,
    ) is not None:
        return 3
    con = _open(db)
    try:
        con.execute(
            _INSERT,
            (
                run_id, step_id, node_id, status,
                artifact_path, artifact_sha256,
                rc, started_at, ended_at,
            ),
        )
        con.commit()
        return 0
    except sqlite3.Error:
        return 2
    finally:
        con.close()


def list_completions_db(db: str, *, run_id: str) -> list[CompletionRow]:
    """Return every completion row for ``run_id`` in recorded_at order.

    Empty list when the run has no completions yet. Always reads through
    the same connection so the caller observes a consistent snapshot.
    """
    con = _open(db)
    try:
        cur = con.execute(_SELECT_FOR_RUN, (run_id,))
        return [CompletionRow(*row) for row in cur.fetchall()]
    finally:
        con.close()


def get_completion_db(db: str, *, run_id: str, step_id: str
                      ) -> Optional[CompletionRow]:
    """Return the single ``(run_id, step_id)`` row, or None when absent.

    Because the table's UNIQUE constraint enforces at most one row per
    pair, this is a point lookup, not a scan. ``fetchone`` directly returns
    the dataclass instance.
    """
    con = _open(db)
    try:
        cur = con.execute(_SELECT_FOR_STEP, (run_id, step_id))
        row = cur.fetchone()
        return CompletionRow(*row) if row is not None else None
    finally:
        con.close()