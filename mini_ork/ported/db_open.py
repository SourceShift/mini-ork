"""Pure-logic port of ``lib/db_open.sh``.

Faithful Python port of the per-connection SQLite pragma wrapper:
``mo_sqlite`` (sqlite3 CLI wrapper with PRAGMA busy_timeout injected
via ``-cmd``) and ``mo_sqlite_py_pragmas`` (emits a Python heredoc
snippet). The bash wrappers in ``lib/db_open.sh`` stay in place
(strangler-fig co-existence); this port gives Python callers an
in-process target and gives tests a stable surface for parity
verification against the live bash.

Parity note (subtle but load-bearing): bash's ``sqlite3 -cmd
"PRAGMA busy_timeout=N;"`` ALWAYS emits the pragma's current value
as the first stdout row before the user's SQL runs. The Python port
mirrors this by capturing the row from ``con.execute("PRAGMA
busy_timeout")`` and prepending it to the result list. Skipping that
row would silently desync stdout shape between the two surfaces
without breaking any individual query.

Public API::

    from mini_ork.ported.db_open import (
        mo_sqlite,              # list[tuple] rows mirroring bash mo_sqlite
        mo_sqlite_py_pragmas,   # str: con.execute("PRAGMA busy_timeout=N")
    )

Env var: ``MO_SQLITE_BUSY_MS`` overrides the default 5000ms on both
sides (unset or empty string = default).
"""

from __future__ import annotations

import os
import sqlite3


_DEFAULT_BUSY_MS = "5000"


def _busy_ms() -> int:
    """Read MO_SQLITE_BUSY_MS, defaulting to 5000 (matches bash)."""
    raw = os.environ.get("MO_SQLITE_BUSY_MS", _DEFAULT_BUSY_MS)
    return int(raw)


def mo_sqlite(db: str, *sql: str) -> list[tuple]:
    """Mirror bash ``mo_sqlite <db> <sql...>``.

    Opens ``db``, applies PRAGMA busy_timeout from ``MO_SQLITE_BUSY_MS``
    (default 5000ms) — the same value bash's ``-cmd`` injects — then
    executes each statement in ``sql`` in order. The first row of the
    returned list is always ``(busy_ms,)`` (the value bash's ``-cmd``
    emits as it sets the pragma); subsequent rows are the fetchall()
    output of any result-bearing user statement (SELECT / PRAGMA read).
    DDL statements (CREATE / ALTER / DROP) contribute no rows, matching
    bash's empty-output behavior for those.

    On no-sql-args the connection still opens and the pragma row is
    returned (matches the bash ``sqlite3 -cmd ... db`` interactive-exit
    semantics — bash emits the pragma value then exits).

    The connection is committed on success and closed on exit; the
    caller does not own it.
    """
    busy_ms = _busy_ms()
    con = sqlite3.connect(db)
    try:
        con.execute(f"PRAGMA busy_timeout={busy_ms}")
        rows: list[tuple] = [con.execute("PRAGMA busy_timeout").fetchone()]
        for stmt in sql:
            cur = con.execute(stmt)
            if cur.description is not None:
                rows.extend(cur.fetchall())
        con.commit()
        return rows
    finally:
        con.close()


def mo_sqlite_py_pragmas() -> str:
    """Mirror bash ``mo_sqlite_py_pragmas()``.

    Returns the Python heredoc snippet a bash caller would inject
    immediately after ``con = sqlite3.connect(sys.argv[1])``::

        con.execute("PRAGMA busy_timeout=5000")

    The format must byte-match what bash printf emits so any caller
    that currently captures the bash output via ``$(mo_sqlite_py_pragmas)``
    into a heredoc gets identical Python source whether bash or Python
    produced the line. Trailing ``\\n`` is preserved (bash's printf
    template includes it; Python's f-string does too).
    """
    return f'con.execute("PRAGMA busy_timeout={_busy_ms()}")\n'