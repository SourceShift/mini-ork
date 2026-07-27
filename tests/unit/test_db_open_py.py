"""Unit tests: ``mini_ork.stores.db_open`` (bash parity halves removed; formerly vs ``lib/db_open.sh``).

For each fixture we seed a self-contained temp DB (via the real
``db/init.sh`` for the round-trip case, or an empty file for the
scalar / float / pragma cases) and call the Python port with a
controlled env, asserting the returned rows semantically.

Row contract: ``mo_sqlite`` echoes the busy_timeout pragma as the first
row, then the user's result rows; NULL comes back as ``None``.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Iterable

import pytest

from mini_ork.stores.db_open import mo_sqlite, mo_sqlite_py_pragmas

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_BUSY_ENV = "MO_SQLITE_BUSY_MS"


# ── In-process helpers ──────────────────────────────────────────────────────


def _with_env(overrides: dict):
    saved = {k: os.environ.pop(k, None) for k in (_BUSY_ENV,)}
    for k, v in overrides.items():
        os.environ[k] = v
    return saved


def _restore_env(saved: dict) -> None:
    for k in (_BUSY_ENV,):
        os.environ.pop(k, None)
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v


def _py_mo_sqlite(db: str, sql: Iterable[str], env_overrides: dict) -> list[tuple]:
    saved = _with_env(env_overrides)
    try:
        return mo_sqlite(db, *sql)
    finally:
        _restore_env(saved)


def _py_mo_sqlite_empty_args(db: str, env_overrides: dict) -> list[tuple]:
    saved = _with_env(env_overrides)
    try:
        return mo_sqlite(db)
    finally:
        _restore_env(saved)


def _py_mo_sqlite_py_pragmas(env_overrides: dict) -> str:
    saved = _with_env(env_overrides)
    try:
        return mo_sqlite_py_pragmas()
    finally:
        _restore_env(saved)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_db(tmp_path) -> str:
    """Empty SQLite file (no schema). Cheap per-test."""
    db = tmp_path / "fresh.db"
    sqlite3.connect(str(db)).close()
    return str(db)


@pytest.fixture(scope="module")
def init_db(tmp_path_factory) -> str:
    """Run real ``db/init.sh`` once per test module, yielding the DB path.

    The init.sh migration graph produces a real schema (>=45 tables,
    views) so the round-trip test exercises the port against the
    production shape, not a hand-rolled fixture. Module scope keeps
    the 48-migration cost amortized across the parametrized cases.
    """
    d = tmp_path_factory.mktemp("db_open_init")
    db = d / "state.db"
    env = os.environ.copy()
    env["MINI_ORK_DB"] = str(db)
    env["MINI_ORK_HOME"] = str(d)
    env["MINI_ORK_ROOT"] = str(REPO_ROOT)
    subprocess.run(
        ["bash", str(REPO_ROOT / "db" / "init.sh")],
        env=env, check=True, cwd=str(REPO_ROOT),
        capture_output=True, text=True,
    )
    return str(db)


# ── mo_sqlite cases ─────────────────────────────────────────────────────────


def test_scalar_select_one(fresh_db):
    """(a) Scalar ``SELECT 1`` on a fresh DB. First row is the busy_timeout
    echo; second row is the user's ``1``."""
    py_rows = _py_mo_sqlite(fresh_db, ["SELECT 1"], {})
    assert py_rows == [(5000,), (1,)]


def test_float_sum(fresh_db):
    """(b) ``SELECT 1.5+2.5`` — exercises the float round-trip."""
    py_rows = _py_mo_sqlite(fresh_db, ["SELECT 1.5+2.5"], {})
    assert py_rows == [(5000,), (4.0,)]


def test_null_value(fresh_db):
    """(b.1) ``SELECT NULL`` — NULL comes back as ``None``."""
    py_rows = _py_mo_sqlite(fresh_db, ["SELECT NULL"], {})
    assert py_rows == [(5000,), (None,)]


def test_multi_statement_roundtrip(init_db):
    """(c) Multi-statement roundtrip on the real init.sh schema:
    CREATE → INSERT → SELECT against a freshly-initialized DB. Proves
    the port works on the production migration graph, not a toy table.
    """
    sqls = [
        # DROP first so the test is idempotent across re-runs sharing
        # the module-scoped init_db.
        "DROP TABLE IF EXISTS _py_probe",
        "CREATE TABLE _py_probe (k INT PRIMARY KEY, v REAL)",
        "INSERT INTO _py_probe (k, v) VALUES (1, 3.14159), (2, 2.71828)",
        "SELECT v FROM _py_probe ORDER BY k",
    ]
    py_rows = _py_mo_sqlite(init_db, sqls, {})
    # busy_timeout echo + 2 SELECT rows = 3; DROP/CREATE/INSERT contribute
    # no rows.
    assert len(py_rows) == 3, (
        f"expected 3 rows (busy_timeout + 2 SELECT); got "
        f"{len(py_rows)}: {py_rows!r}"
    )
    assert py_rows[0] == (5000,), f"busy_timeout echo row drift: {py_rows!r}"
    assert py_rows[-2] == (3.14159,), f"row 1 float drift: {py_rows!r}"
    assert py_rows[-1] == (2.71828,), f"row 2 float drift: {py_rows!r}"


def test_busy_timeout_override(init_db):
    """(d) ``MO_SQLITE_BUSY_MS=1234`` override. The busy_timeout row
    (always first) must echo 1234; a SELECT roundtrip confirms the
    override doesn't break result-bearing statements.
    """
    env_overrides = {_BUSY_ENV: "1234"}
    sqls = ["SELECT 42"]
    py_rows = _py_mo_sqlite(init_db, sqls, env_overrides)
    assert py_rows[0] == (1234,), (
        f"busy_timeout override not honored on first row: {py_rows!r}"
    )
    assert py_rows[1] == (42,), (
        f"user SELECT row drift: {py_rows!r}"
    )


def test_no_sql_args(fresh_db):
    """(e) ``mo_sqlite <db>`` with no SQL — the pragma's echoed value is
    the only row returned."""
    py_rows = _py_mo_sqlite_empty_args(fresh_db, {})
    assert py_rows == [(5000,)], (
        f"no-sql-args default should emit single 5000 row; got {py_rows!r}"
    )


# ── mo_sqlite_py_pragmas cases ──────────────────────────────────────────────


def test_py_pragmas_default():
    """(f) Default busy_ms: emits exactly the byte-string contract —
    including the trailing ``\\n`` so any heredoc-injecting caller gets
    identical Python source."""
    py_out = _py_mo_sqlite_py_pragmas({})
    assert py_out == 'con.execute("PRAGMA busy_timeout=5000")\n', (
        f"default pragma drift: py={py_out!r}"
    )


def test_py_pragmas_override():
    """(g) ``MO_SQLITE_BUSY_MS=1234`` override: same byte-string
    contract, just with the override baked in."""
    env_overrides = {_BUSY_ENV: "1234"}
    py_out = _py_mo_sqlite_py_pragmas(env_overrides)
    assert py_out == 'con.execute("PRAGMA busy_timeout=1234")\n', (
        f"override pragma drift: py={py_out!r}"
    )


# ── Smoke ───────────────────────────────────────────────────────────────────


def test_smoke_import_no_io():
    """Module imports cleanly; public API is callable with no DB I/O."""
    import mini_ork.stores.db_open as mod
    assert mod.mo_sqlite.__name__ == "mo_sqlite"
    assert mod.mo_sqlite_py_pragmas.__name__ == "mo_sqlite_py_pragmas"
    assert callable(mod.mo_sqlite)
    assert callable(mod.mo_sqlite_py_pragmas)
