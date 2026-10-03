"""Read-only SQLite opener that survives an idle WAL database.

``state.db`` runs in WAL mode. A ``mode=ro`` URI connection cannot create
the ``-shm`` file, so when no process has the database open and the
``-wal``/``-shm`` sidecar files are absent — SQLite's default on Linux
deletes them when the last connection closes; on macOS other tools can
leave the same state — every query on a read-only connection fails with
``sqlite3.OperationalError: unable to open database file``. Reproduced 2026-10-03 on ``StateDB``
(the reader behind ``mini-ork serve``, the ACP run history and the
``mcp-context`` tools): every query raised in that state.

This module is the one definition of "open state.db read-only without
loud-failing on an idle WAL". Callers replace the f-string URI pattern
with :func:`connect_readonly` and keep their own row factories, pragmas,
isolation levels, and error handling untouched.

The two-step strategy:

1. **Prefer ``mode=ro``** (``Path.resolve().as_uri() + "?mode=ro"``).
   ``as_uri`` percent-encodes ``?``, ``#``, ``%`` and spaces; the old
   f-string patterns broke on paths with reserved characters.
2. **Probe with ``PRAGMA schema_version``.**
   The ``-shm`` failure surfaces here even when ``sqlite3.connect`` would
   defer it. On ``OperationalError`` the read-only handle is replaced by:
3. **A plain connect + ``PRAGMA query_only = ON``.**
   A plain connection CAN create the ``-shm``/``-wal`` sidecar files
   (the writer process already manages them; harmless in this repo's
   layout). ``query_only = ON`` keeps it strictly read-only, so the
   fallback path also cannot write.

A path that does not exist raises ``FileNotFoundError`` rather than
creating an empty database (the plain-connect fallback would otherwise
do so, which is why a missing db is loud and a present-but-idle db is
quiet).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

__all__ = ["connect_readonly"]


def connect_readonly(
    path: str | os.PathLike, *, timeout: float = 5.0
) -> sqlite3.Connection:
    """Open ``path`` for read-only access; survive an idle WAL database.

    Tries ``mode=ro`` first; on ``sqlite3.OperationalError`` (the WAL
    sidecar file is absent) falls back to a sidecar-creating plain
    connection with ``PRAGMA query_only = ON``. A missing path raises
    ``FileNotFoundError`` and creates nothing.

    The caller sets ``isolation_level``, ``row_factory``, and any other
    PRAGMAs after the call. ``isolation_level`` defaults to sqlite3's
    deferred-transaction value (``""``); only :class:`mini_ork.web.db.StateDB`
    overrides it (``None`` for autocommit), and it does so at the call
    site.

    Args:
        path: Filesystem path to the database. May be absolute or
            relative; ``Path.resolve()`` is applied (follows symlinks,
            idempotent on already-resolved paths).
        timeout: Seconds to wait for the database lock on each connect
            attempt. Default 5s — matches the rest of the runtime
            (``cost_ledger.py``:43, ``StateDB._conn_for_thread``:55).

    Returns:
        An open ``sqlite3.Connection``. The caller is responsible for
        ``close()`` (typically via a ``try/finally`` or a context
        manager).

    Raises:
        FileNotFoundError: ``path`` does not exist.
        sqlite3.OperationalError: The database exists and is not
            readable as a SQLite file (corrupt, wrong format, etc.).
        sqlite3.DatabaseError: Other SQLite errors not handled by the
            fallback ladder.
    """
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"sqlite database not found: {resolved}")

    uri = resolved.as_uri() + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=timeout)
        # Probe — the WAL sidecar failure surfaces here even on a
        # connect() that "succeeded". ``as_uri()`` percent-encodes
        # reserved characters; the probe is the cheapest smoke test
        # that the encoding round-trip is valid for sqlite3 too.
        con.execute("PRAGMA schema_version").fetchone()
        return con
    except sqlite3.OperationalError:
        # The read-only handle failed: either the ``-shm`` is absent
        # (idle WAL) or the path's encoding tricked sqlite3. Close it,
        # fall back to a plain connection (which can create the
        # sidecar) with ``query_only`` to keep it read-only.
        try:
            con.close()  # type: ignore[possibly-undefined]
        except (NameError, sqlite3.Error, OSError):
            pass

        try:
            fallback = sqlite3.connect(os.fspath(resolved), timeout=timeout)
            fallback.execute("PRAGMA query_only = ON")
            return fallback
        except sqlite3.OperationalError:
            # Both paths failed. Re-raise so the caller can decide
            # whether to treat it as soft (existing ``except
            # sqlite3.Error`` arms will catch this) or surface it.
            raise