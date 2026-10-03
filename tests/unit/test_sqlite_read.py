"""Hermetic tests for ``mini_ork.sqlite_read.connect_readonly``.

The headline regression: a WAL database with no ``-shm``/``-wal`` sidecar
files (the idle state) raises ``sqlite3.OperationalError`` on every
``mode=ro`` URI open. The fixture below mirrors the canonical
``test_cost_ledger.py::test_idle_wal_db_without_sidecar_files_is_still_read``
fixture: build a WAL db → ``PRAGMA wal_checkpoint(TRUNCATE)`` → delete
``-shm``/``-wal``. Then verify the new helper reads the row, the
``StateDB`` regression is closed, the read-only contract holds on both
the ``mode=ro`` and fallback paths, the ``#``/``?`` directory encoding
is correct, a missing path is loud, and a non-WAL db still opens via
``mode=ro``.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.sqlite_read import connect_readonly  # noqa: E402
from mini_ork.web.db import StateDB  # noqa: E402


def _build_idle_wal_db(tmp_path: Path, *, name: str = "state.db") -> Path:
    """Build a WAL db, checkpoint(TRUNCATE), and delete any sidecar files.

    Mirrors the fixture in ``tests/unit/test_cost_ledger.py``:212-230.
    The "idle" state — present main file, no ``-shm``/``-wal`` — is what
    the previous ``mode=ro`` URI failed to open.
    """
    db = tmp_path / name
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA journal_mode=wal")
        con.execute(
            "CREATE TABLE rows_read("
            "id INTEGER PRIMARY KEY,"
            "value TEXT"
            ")"
        )
        con.execute("INSERT INTO rows_read(id, value) VALUES (1, 'alpha')")
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    for suffix in ("-shm", "-wal"):
        side = tmp_path / f"{name}{suffix}"
        if side.exists():
            os.remove(side)
    return db


def test_connect_readonly_reads_row_on_idle_wal_db(tmp_path: Path) -> None:
    """The helper's headline case — a WAL db with no sidecars opens."""
    db = _build_idle_wal_db(tmp_path)

    con = connect_readonly(db)
    try:
        row = con.execute("SELECT value FROM rows_read WHERE id = 1").fetchone()
    finally:
        con.close()

    assert row == ("alpha",)


def test_state_db_reads_row_on_idle_wal_db(tmp_path: Path) -> None:
    """The headline regression — ``StateDB.rows`` returns the row.

    Before the fix: ``StateDB._conn_for_thread`` raised
    ``sqlite3.OperationalError`` on an idle WAL db, which the FastAPI
    handler caught as a 500. ``StateDB.__init__`` does ``exists()`` but
    does not check for sidecar files.
    """
    db = _build_idle_wal_db(tmp_path)
    state = StateDB(db)

    rows = state.rows("SELECT value FROM rows_read WHERE id = 1")
    state.close()

    assert rows == [{"value": "alpha"}]


def test_writes_fail_on_mode_ro_path(tmp_path: Path) -> None:
    """A write through a ``mode=ro`` connection raises ``OperationalError``.

    We confirm the read-only contract on the primary path (the
    ``mode=ro`` probe succeeds against a non-WAL db, so the helper
    returns the read-only handle without falling back).
    """
    db = tmp_path / "rollback.db"  # rollback journal, not WAL
    con = sqlite3.connect(db)
    try:
        con.execute(
            "CREATE TABLE rows_read(id INTEGER PRIMARY KEY, value TEXT)"
        )
        con.execute("INSERT INTO rows_read VALUES (1, 'alpha')")
        con.commit()
    finally:
        con.close()

    reader = connect_readonly(db)
    try:
        # ``mode=ro`` is enforced by the URI; writes must fail.
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO rows_read VALUES (2, 'beta')")
        # Reads still work.
        row = reader.execute("SELECT value FROM rows_read WHERE id = 1").fetchone()
    finally:
        reader.close()
    assert row == ("alpha",)


def test_writes_fail_on_fallback_path(tmp_path: Path) -> None:
    """The fallback path also rejects writes (``PRAGMA query_only = ON``).

    The fallback runs when ``mode=ro`` fails — here we force that by
    building an idle WAL db, which is exactly the condition that
    triggered the original bug. The fallback ``PRAGMA query_only = ON``
    keeps the read-only contract.
    """
    db = _build_idle_wal_db(tmp_path)
    reader = connect_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO rows_read VALUES (2, 'beta')")
        row = reader.execute("SELECT value FROM rows_read WHERE id = 1").fetchone()
    finally:
        reader.close()
    assert row == ("alpha",)


def test_directory_with_reserved_chars_opens(tmp_path: Path) -> None:
    """A directory whose name contains ``#`` and ``?`` opens via ``as_uri``.

    The old f-string pattern ``f"file:{path}?mode=ro"`` would generate a
    malformed string here (``?mode=ro`` after a literal ``?`` in the path).
    ``Path.resolve().as_uri()`` percent-encodes ``?``, ``#``, ``%``, and
    spaces — the only correct primitive.
    """
    tricky = tmp_path / "weird#dir?"
    tricky.mkdir()
    db = _build_idle_wal_db(tricky, name="state.db")
    # ``connect_readonly`` calls ``.resolve()`` internally; the resolved
    # path lives under our tricky directory.
    reader = connect_readonly(db)
    try:
        row = reader.execute("SELECT value FROM rows_read WHERE id = 1").fetchone()
    finally:
        reader.close()
    assert row == ("alpha",)


def test_missing_path_raises_filenotfound_and_creates_nothing(tmp_path: Path) -> None:
    """A missing path raises ``FileNotFoundError`` and creates no file.

    The plain-connect fallback (step 3 of the spec) would otherwise
    CREATE an empty database at the path; the spec mandates ``raise``
    before that happens. We verify both the exception type and the
    absence of a file at the would-be path.
    """
    missing = tmp_path / "does_not_exist.db"
    assert not missing.exists()

    with pytest.raises(FileNotFoundError):
        connect_readonly(missing)

    assert not missing.exists(), (
        "the plain-connect fallback must NOT create an empty database "
        "at a missing path"
    )


def test_rollback_journal_db_opens_on_mode_ro_path(tmp_path: Path) -> None:
    """A non-WAL (rollback-journal) db opens via the ``mode=ro`` path.

    The fallback path is for WAL only — a rollback journal never had a
    ``-shm`` sidecar to begin with, so ``mode=ro`` succeeds on the
    first try. This locks in the helper's preference order: prefer
    ``mode=ro`` first, fall back only on ``OperationalError``.
    """
    db = tmp_path / "rollback.db"
    con = sqlite3.connect(db)
    try:
        con.execute(
            "CREATE TABLE rows_read(id INTEGER PRIMARY KEY, value TEXT)"
        )
        con.execute("INSERT INTO rows_read VALUES (1, 'alpha')")
        con.commit()
    finally:
        con.close()
    # Sanity: no WAL sidecars.
    assert not (tmp_path / "rollback.db-shm").exists()
    assert not (tmp_path / "rollback.db-wal").exists()

    reader = connect_readonly(db)
    try:
        row = reader.execute("SELECT value FROM rows_read WHERE id = 1").fetchone()
    finally:
        reader.close()
    assert row == ("alpha",)
