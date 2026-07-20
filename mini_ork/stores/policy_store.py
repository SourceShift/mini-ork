"""Central backend selector for mini-ork brain libraries — Python port of lib/policy_store.sh.

The store-port seam that ``lane_router``, ``process_reward``, and
``gradient_extractor`` go through to learn whether the live store is
SQLite (default; eng-team, preserved behavior) or Postgres (v0.2-pt36
stub, intentionally aborting before any sqlite3 call so the real
researcher-side PG impl can swap the stub without re-architecting
callers).

Co-existence model (strangler-fig): bash ``lib/policy_store.sh`` is the
authoritative source and stays untouched. This module mirrors its
public API 1:1 so Python callers get an in-process surface and
``tests/unit/test_policy_store_py.py`` gets a stable target to byte-diff
against the live bash subprocess.

Public API (bash function → Python):
  mo_store_backend                 → backend() -> str
  mo_store_assert_sqlite           → assert_sqlite() -> None
  mo_store_db_path                 → db_path() -> str
  mo_store_py_connect_snippet      → py_connect_snippet() -> str
  mo_store_py_pragmas_snippet      → py_pragmas_snippet() -> str

Exit-code parity: bash returns 64 (EX_USAGE) for an unknown
``MO_STORE_BACKEND`` and 78 (EX_CONFIG) for ``assert_sqlite`` on a
non-sqlite backend. The Python port raises ``SystemExit(N)`` with the
same integer so subprocess-style callers see an identical rc. Bare
``ValueError`` / ``Exception`` would silently diverge — a caller doing
``subprocess.run(..., check=False)`` on a Python wrapper would see rc=1
instead of 64/78.

Snippet byte-exactness: bash emits snippet lines via
``printf '<literal>\\n'``. The Python port returns the EXACT same string
(e.g. ``'import sqlite3\\ncon = sqlite3.connect(db)\\n'`` for sqlite,
``'con.execute("PRAGMA busy_timeout=5000")\\n'`` for sqlite pragma).
Returning as a string — NOT via ``print()`` — avoids Python's implicit
trailing newline breaking byte-for-byte parity against the live bash
subprocess.

Default-value parity: bash uses ``${VAR:-default}`` (fires when VAR is
unset OR empty). Python's ``os.environ.get("VAR", "default")`` only
fires when VAR is unset. We use ``os.environ.get("VAR") or "default"``
so empty-string env vars collapse to the default the same way they do
in bash.
"""
from __future__ import annotations

import os
import sys

__all__ = [
    "backend",
    "assert_sqlite",
    "db_path",
    "py_connect_snippet",
    "py_pragmas_snippet",
]


def backend() -> str:
    """Return the current backend name (``"sqlite"`` or ``"postgres"``).

    Mirrors ``mo_store_backend``. Unknown values raise ``SystemExit(64)``
    so a typo doesn't silently fall back to SQLite and mask a
    misconfigured deployment.
    """
    b = os.environ.get("MO_STORE_BACKEND") or "sqlite"
    if b == "sqlite":
        return "sqlite"
    if b == "postgres":
        return "postgres"
    sys.stderr.write(
        f"policy_store: unknown MO_STORE_BACKEND={b} (expected sqlite|postgres)\n"
    )
    raise SystemExit(64)


def db_path() -> str:
    """Return the resolved DB file path.

    Mirrors ``mo_store_db_path``. Resolution order matches the
    historical ``STATE_DB`` convention in ``lane_router`` /
    ``process_reward`` so SQLite-default callers see no change::

        MO_STORE_DB → MINI_ORK_DB → ${MINI_ORK_HOME}/state.db
                                       → ${PWD}/.mini-ork/state.db

    Returned string has NO trailing newline (bash's ``printf '%s\\n'``
    trailing newline is added by the caller at the shell boundary; the
    Python port keeps the value clean for programmatic use).
    """
    mo_store_db = os.environ.get("MO_STORE_DB") or ""
    if mo_store_db:
        return mo_store_db
    mini_ork_db = os.environ.get("MINI_ORK_DB") or ""
    if mini_ork_db:
        return mini_ork_db
    mini_ork_home = os.environ.get("MINI_ORK_HOME") or ""
    if mini_ork_home:
        return f"{mini_ork_home}/state.db"
    return f"{os.getcwd()}/.mini-ork/state.db"


def assert_sqlite() -> None:
    """Gate SQLite-direct callers behind the seam.

    Mirrors ``mo_store_assert_sqlite``. Raises ``SystemExit(78)`` when
    a non-sqlite backend is selected so a Postgres caller never
    silently executes against the local ``.mini-ork/state.db``. Returns
    ``None`` on the sqlite happy path (no output, mirroring bash's
    rc=0 + silent exit).
    """
    b = backend()
    if b != "sqlite":
        sys.stderr.write(
            f"policy_store: backend={b} is a stub in v0.2-pt36; "
            "real impl lands researcher-side. "
            "Set MO_STORE_BACKEND=sqlite for default behavior.\n"
        )
        raise SystemExit(78)
    return None


def py_connect_snippet() -> str:
    """Return backend-aware Python connect code for ``python3 - <<PY`` heredocs.

    Mirrors ``mo_store_py_connect_snippet``. SQLite emits the canonical
    ``sqlite3.connect(db)`` open. Postgres emits a ``SystemExit`` before
    any DB call so the real PG impl can replace the stub by editing
    this single function.

    Returned string is byte-exact with bash's ``printf '%s\\n'`` output
    — callers can substitute via ``subprocess.run(..., text=True).stdout``
    or compare directly in tests.
    """
    b = backend()
    if b == "sqlite":
        return "import sqlite3\ncon = sqlite3.connect(db)\n"
    if b == "postgres":
        return (
            'raise SystemExit("policy_store: backend=postgres is a stub in '
            "v0.2-pt36 (researcher-side impl pending). "
            'Aborting before any PG call.")\n'
        )
    return ""


def py_pragmas_snippet() -> str:
    """Return backend-aware Python pragma code for ``python3 - <<PY`` heredocs.

    Mirrors ``mo_store_py_pragmas_snippet``. For SQLite this sets
    ``PRAGMA busy_timeout`` per-connection (see F-11/R1 audit note in
    ``lib/db_open.sh``); for Postgres it's a no-op — the connect snippet
    above aborts before this line runs.
    """
    b = backend()
    if b == "sqlite":
        busy_ms = os.environ.get("MO_SQLITE_BUSY_MS") or "5000"
        return f'con.execute("PRAGMA busy_timeout={busy_ms}")\n'
    if b == "postgres":
        return ""
    return ""