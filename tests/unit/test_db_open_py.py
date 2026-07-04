"""Parity gate: ``mini_ork.ported.db_open`` vs ``lib/db_open.sh``.

For each fixture we seed a self-contained temp DB (via the real
``db/init.sh`` for the round-trip case, or an empty file for the
scalar / float / pragma cases), invoke the LIVE bash functions via
subprocess (no mocking), then call the Python port with the SAME
env via in-process capture and compare row-by-row.

Why row-by-row (not raw stdout): bash's ``sqlite3 -cmd "PRAGMA
busy_timeout=N;" db SQL`` emits one line per cell with the default
``|`` column separator and NULL→empty-string, while Python's
``fetchall()`` returns a list of tuples with None for NULL and
typed values for ints/floats. A naive byte-equality compare would
mask real semantic parity (or worse, false-fail on cosmetic
differences); the cell-by-cell compare with float-tolerance
preserves the parity contract: ``|bash_row - py_row| < 1e-6`` per
cell.

NULL handling: bash prints ``""`` (empty string) for NULL;
Python gives ``None``. The harness coerces bash empty cells to
``None`` before compare.

Strangler-fig co-existence is preserved: ``lib/db_open.sh`` is
byte-identical before and after this test file exists.
"""

from __future__ import annotations

import math
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Iterable

import pytest

from mini_ork.ported.db_open import mo_sqlite, mo_sqlite_py_pragmas

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIB_DB_OPEN = REPO_ROOT / "lib" / "db_open.sh"

_FLOAT_TOL = 1e-6
_BUSY_ENV = "MO_SQLITE_BUSY_MS"


# ── Subprocess + in-process helpers ─────────────────────────────────────────


def _build_env(overrides: dict) -> dict:
    env = os.environ.copy()
    env.pop(_BUSY_ENV, None)
    env.update(overrides)
    return env


def _bash_mo_sqlite(db: str, sql: Iterable[str], env: dict) -> str:
    quoted_db = f'"{db}"'
    quoted_sql = " ".join(f'"{s}"' for s in sql)
    proc = subprocess.run(
        ["bash", "-c",
         f'. "{LIB_DB_OPEN}" && mo_sqlite {quoted_db} {quoted_sql}'],
        cwd=str(REPO_ROOT), env=env, check=True,
        capture_output=True, text=True,
    )
    return proc.stdout


def _bash_mo_sqlite_empty_args(db: str, env: dict) -> str:
    proc = subprocess.run(
        ["bash", "-c",
         f'. "{LIB_DB_OPEN}" && mo_sqlite "{db}"'],
        cwd=str(REPO_ROOT), env=env, check=True,
        capture_output=True, text=True,
    )
    return proc.stdout


def _bash_mo_sqlite_py_pragmas(env: dict) -> str:
    proc = subprocess.run(
        ["bash", "-c",
         f'. "{LIB_DB_OPEN}" && mo_sqlite_py_pragmas'],
        cwd=str(REPO_ROOT), env=env, check=True,
        capture_output=True, text=True,
    )
    return proc.stdout


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


# ── Row-shape coercion + parity assert ──────────────────────────────────────


def _coerce_cell(s: str):
    """Coerce a bash sqlite3 CLI cell string to a comparable Python value.

    Bash default: NULL→"", ints/floats as decimal text, text as-is.
    Python fetchall: None / int / float / str. This mirrors them.
    """
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _parse_bash_rows(stdout: str) -> list[tuple]:
    rows: list[tuple] = []
    # Split on newlines but PRESERVE empty cells (NULL rows are
    # empty lines in sqlite3 CLI output). The trailing ``\\n`` makes
    # split() yield a final empty segment — drop only that one, not
    # any NULL row in the middle.
    parts = stdout.split("\n")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    for line in parts:
        cells = line.split("|")
        rows.append(tuple(_coerce_cell(c) for c in cells))
    return rows


def _assert_rows_parity(bash_stdout: str, py_rows: list[tuple], ctx: str) -> None:
    bash_rows = _parse_bash_rows(bash_stdout)
    assert len(bash_rows) == len(py_rows), (
        f"[{ctx}] row count drift: bash={bash_rows!r} py={py_rows!r}"
    )
    for i, (br, pr) in enumerate(zip(bash_rows, py_rows)):
        assert len(br) == len(pr), (
            f"[{ctx}] row {i} col count drift: bash={br!r} py={pr!r}"
        )
        for j, (bc, pc) in enumerate(zip(br, pr)):
            if bc is None:
                assert pc is None, (
                    f"[{ctx}] row {i} col {j} NULL mismatch: "
                    f"bash={bc!r} py={pc!r}"
                )
            elif isinstance(bc, float) or isinstance(pc, float):
                assert math.isclose(float(bc), float(pc), abs_tol=_FLOAT_TOL), (
                    f"[{ctx}] row {i} col {j} float drift: "
                    f"bash={bc!r} py={pc!r} tol={_FLOAT_TOL}"
                )
            else:
                assert bc == pc, (
                    f"[{ctx}] row {i} col {j} value drift: "
                    f"bash={bc!r} py={pc!r}"
                )


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


# ── mo_sqlite parity cases ──────────────────────────────────────────────────


def test_scalar_select_one_parity(fresh_db):
    """(a) Scalar ``SELECT 1`` on a fresh DB. First row is the busy_timeout
    echo; second row is the user's ``1``."""
    bash_out = _bash_mo_sqlite(fresh_db, ["SELECT 1"], _build_env({}))
    py_rows = _py_mo_sqlite(fresh_db, ["SELECT 1"], {})
    _assert_rows_parity(bash_out, py_rows, "scalar_select_one")


def test_float_sum_parity(fresh_db):
    """(b) ``SELECT 1.5+2.5`` — exercises float round-trip with the 1e-6
    tolerance floor. Both sides must agree within tolerance on each
    cell of each row."""
    bash_out = _bash_mo_sqlite(fresh_db, ["SELECT 1.5+2.5"], _build_env({}))
    py_rows = _py_mo_sqlite(fresh_db, ["SELECT 1.5+2.5"], {})
    _assert_rows_parity(bash_out, py_rows, "float_sum_1.5+2.5")


def test_null_value_parity(fresh_db):
    """(b.1) ``SELECT NULL`` — exercises NULL→empty vs None coercion.
    Without the cell-level coerce step the test would false-fail on
    bash emitting ``""`` and Python emitting ``None``."""
    bash_out = _bash_mo_sqlite(fresh_db, ["SELECT NULL"], _build_env({}))
    py_rows = _py_mo_sqlite(fresh_db, ["SELECT NULL"], {})
    _assert_rows_parity(bash_out, py_rows, "null_value")


def test_multi_statement_roundtrip_parity(init_db):
    """(c) Multi-statement roundtrip on the real init.sh schema:
    CREATE → INSERT → SELECT against a freshly-initialized DB. Proves
    the port works on the production migration graph, not a toy table.
    """
    sqls = [
        # DROP first so the test is idempotent across re-runs sharing
        # the module-scoped init_db (bash subprocess runs first and
        # leaves the table on disk; without DROP the Python CREATE
        # collides).
        "DROP TABLE IF EXISTS _py_probe",
        "CREATE TABLE _py_probe (k INT PRIMARY KEY, v REAL)",
        "INSERT INTO _py_probe (k, v) VALUES (1, 3.14159), (2, 2.71828)",
        "SELECT v FROM _py_probe ORDER BY k",
    ]
    bash_out = _bash_mo_sqlite(init_db, sqls, _build_env({}))
    py_rows = _py_mo_sqlite(init_db, sqls, {})
    _assert_rows_parity(bash_out, py_rows, "multi_statement_roundtrip")
    # busy_timeout echo + 2 SELECT rows = 3; DROP/CREATE/INSERT contribute
    # no rows on either side.
    assert len(py_rows) == 3, (
        f"expected 3 rows (busy_timeout + 2 SELECT); got "
        f"{len(py_rows)}: {py_rows!r}"
    )
    assert py_rows[0] == (5000,), f"busy_timeout echo row drift: {py_rows!r}"
    assert py_rows[-2] == (3.14159,), f"row 1 float drift: {py_rows!r}"
    assert py_rows[-1] == (2.71828,), f"row 2 float drift: {py_rows!r}"


def test_busy_timeout_override_parity(init_db):
    """(d) ``MO_SQLITE_BUSY_MS=1234`` override. The busy_timeout row
    (always first) must echo 1234 on both sides; a SELECT roundtrip
    confirms the override doesn't break result-bearing statements.
    """
    env_overrides = {_BUSY_ENV: "1234"}
    sqls = ["SELECT 42"]
    bash_out = _bash_mo_sqlite(init_db, sqls, _build_env(env_overrides))
    py_rows = _py_mo_sqlite(init_db, sqls, env_overrides)
    _assert_rows_parity(bash_out, py_rows, "busy_timeout_override")
    assert py_rows[0] == (1234,), (
        f"busy_timeout override not honored on first row: {py_rows!r}"
    )
    assert py_rows[1] == (42,), (
        f"user SELECT row drift: {py_rows!r}"
    )


def test_no_sql_args_parity(fresh_db):
    """(e) ``mo_sqlite <db>`` with no SQL — bash opens, applies the
    pragma via ``-cmd``, and exits; the pragma's echoed value is the
    only stdout. Python mirrors this by returning the pragma row."""
    bash_out = _bash_mo_sqlite_empty_args(fresh_db, _build_env({}))
    py_rows = _py_mo_sqlite_empty_args(fresh_db, {})
    _assert_rows_parity(bash_out, py_rows, "no_sql_args")
    assert py_rows == [(5000,)], (
        f"no-sql-args default should emit single 5000 row; got {py_rows!r}"
    )


# ── mo_sqlite_py_pragmas parity cases ───────────────────────────────────────


def test_py_pragmas_default_parity():
    """(f) Default busy_ms: emits exactly the byte-string bash printf
    produces — including the trailing ``\\n`` so any heredoc-injecting
    caller gets identical Python source."""
    bash_out = _bash_mo_sqlite_py_pragmas(_build_env({}))
    py_out = _py_mo_sqlite_py_pragmas({})
    assert bash_out == py_out == 'con.execute("PRAGMA busy_timeout=5000")\n', (
        f"default pragma drift: bash={bash_out!r} py={py_out!r}"
    )


def test_py_pragmas_override_parity():
    """(g) ``MO_SQLITE_BUSY_MS=1234`` override: same byte-string
    contract, just with the override baked in."""
    env_overrides = {_BUSY_ENV: "1234"}
    bash_out = _bash_mo_sqlite_py_pragmas(_build_env(env_overrides))
    py_out = _py_mo_sqlite_py_pragmas(env_overrides)
    assert bash_out == py_out == 'con.execute("PRAGMA busy_timeout=1234")\n', (
        f"override pragma drift: bash={bash_out!r} py={py_out!r}"
    )


# ── Smoke ───────────────────────────────────────────────────────────────────


def test_smoke_import_no_io():
    """Module imports cleanly; public API is callable with no DB I/O."""
    import mini_ork.ported.db_open as mod
    assert mod.mo_sqlite.__name__ == "mo_sqlite"
    assert mod.mo_sqlite_py_pragmas.__name__ == "mo_sqlite_py_pragmas"
    assert callable(mod.mo_sqlite)
    assert callable(mod.mo_sqlite_py_pragmas)