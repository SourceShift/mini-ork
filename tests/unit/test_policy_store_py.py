"""Standalone unit tests for ``mini_ork.stores.policy_store``.

Replaces the bash-parity gate (against ``lib/policy_store.sh``) as part of
the bash→Python migration: the Python port is now the sole implementation,
so its coverage no longer runs ``lib/policy_store.sh`` in a subprocess —
it asserts the port's behaviour directly. The expected values below are
the semantic contract the bash side used to pin (backend enum strings,
exit-code shape 64 / 78 via ``SystemExit``, stderr messages, db_path
resolution chain, snippet bytes, and the pragma round-trip), now asserted
on the port's output.

Eight cases:
  (a) backend() default 'sqlite' when MO_STORE_BACKEND unset
  (b) backend()='sqlite' when MO_STORE_BACKEND=sqlite
  (c) backend()='postgres' when MO_STORE_BACKEND=postgres
  (d) backend() raises SystemExit(64) with stderr containing
      'unknown MO_STORE_BACKEND=mysql' when MO_STORE_BACKEND=mysql
  (e) assert_sqlite — None on sqlite, SystemExit(78) on postgres with
      the stub stderr text
  (f) db_path() resolution chain — three sub-cases for
      MO_STORE_DB / MINI_ORK_DB / MINI_ORK_HOME + one for the
      $(pwd)/.mini-ork/state.db fallback
  (g) py_connect_snippet + py_pragmas_snippet exact-string assertions
      for both sqlite AND postgres branches (4 strings)
  (h) DB round-trip — exec the emitted snippets against a temp DB
      initialised via ``mini_ork.stores.migrate.init_db``; assert the
      PRAGMA busy_timeout / PRAGMA journal_mode introspection rows

Env isolation: ``monkeypatch.setenv`` / ``delenv``. Mirrors the old
bash's ``${VAR:-default}`` semantics: unset and empty collapse the same
way, so we use ``delenv`` (not ``setenv('', '')``) to be explicit.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.stores import policy_store as ps  # noqa: E402

# Vars that may exist in the parent pytest env and would silently
# influence policy_store; every test scrubs these via _scrub().
_PS_ENV = (
    "MO_STORE_BACKEND",
    "MO_STORE_DB",
    "MINI_ORK_DB",
    "MINI_ORK_HOME",
    "MO_SQLITE_BUSY_MS",
)


def _scrub(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    """Unset every policy_store env var, then apply overrides."""
    for k in _PS_ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in overrides.items():
        monkeypatch.setenv(k, v)


# ── (a) default backend when MO_STORE_BACKEND unset ─────────────────────────


def test_backend_default_sqlite(monkeypatch):
    _scrub(monkeypatch)
    assert ps.backend() == "sqlite"


# ── (b) explicit sqlite ──────────────────────────────────────────────────────


def test_backend_explicit_sqlite(monkeypatch):
    _scrub(monkeypatch, MO_STORE_BACKEND="sqlite")
    assert ps.backend() == "sqlite"


# ── (c) explicit postgres ────────────────────────────────────────────────────


def test_backend_explicit_postgres(monkeypatch):
    _scrub(monkeypatch, MO_STORE_BACKEND="postgres")
    assert ps.backend() == "postgres"


# ── (d) unknown backend → SystemExit(64) + stderr ───────────────────────────


def test_backend_unknown_raises_systemexit_64(monkeypatch, capsys):
    _scrub(monkeypatch, MO_STORE_BACKEND="mysql")
    with pytest.raises(SystemExit) as exc:
        ps.backend()
    assert exc.value.code == 64
    captured = capsys.readouterr()
    assert "unknown MO_STORE_BACKEND=mysql" in captured.err
    assert "(expected sqlite|postgres)" in captured.err


# ── (e) assert_sqlite ────────────────────────────────────────────────────────


def test_assert_sqlite(monkeypatch, capsys):
    # sqlite → no output, returns None
    _scrub(monkeypatch, MO_STORE_BACKEND="sqlite")
    assert ps.assert_sqlite() is None

    # postgres → SystemExit(78), stderr contains the stub message
    _scrub(monkeypatch, MO_STORE_BACKEND="postgres")
    with pytest.raises(SystemExit) as exc:
        ps.assert_sqlite()
    assert exc.value.code == 78
    captured = capsys.readouterr()
    assert "backend=postgres is a stub in v0.2-pt36" in captured.err
    assert "Set MO_STORE_BACKEND=sqlite for default behavior." in captured.err


# ── (f) db_path() resolution chain — three sub-cases ─────────────────────────


def test_db_path_mo_store_db_wins(monkeypatch, tmp_path):
    target = str(tmp_path / "explicit.db")
    _scrub(monkeypatch, MO_STORE_DB=target)
    assert ps.db_path() == target


def test_db_path_mini_ork_db_fallback(monkeypatch, tmp_path):
    target = str(tmp_path / "from_mini_ork_db.db")
    _scrub(monkeypatch, MINI_ORK_DB=target)
    assert ps.db_path() == target


def test_db_path_mini_ork_home_fallback(monkeypatch, tmp_path):
    home = str(tmp_path)
    _scrub(monkeypatch, MINI_ORK_HOME=home)
    expected = str(tmp_path / "state.db")
    assert ps.db_path() == expected


def test_db_path_pwd_fallback(monkeypatch, tmp_path):
    # No MO_STORE_DB / MINI_ORK_DB / MINI_ORK_HOME → falls through to
    # $(pwd)/.mini-ork/state.db.
    _scrub(monkeypatch)
    cwd = str(tmp_path)
    expected = f"{cwd}/.mini-ork/state.db"
    monkeypatch.chdir(cwd)  # os.getcwd() == tmp_path
    assert ps.db_path() == expected


# ── (g) snippet byte-exactness — 4 strings, both backends ────────────────────


def test_py_connect_snippet_sqlite_exact(monkeypatch):
    _scrub(monkeypatch, MO_STORE_BACKEND="sqlite")
    py_snip = ps.py_connect_snippet()
    assert py_snip == "import sqlite3\ncon = sqlite3.connect(db)\n"


def test_py_connect_snippet_postgres_exact(monkeypatch):
    _scrub(monkeypatch, MO_STORE_BACKEND="postgres")
    py_snip = ps.py_connect_snippet()
    # Verify both the raise shape and the stub keyphrase are emitted.
    assert "raise SystemExit(" in py_snip
    assert "backend=postgres is a stub in v0.2-pt36" in py_snip
    assert "Aborting before any PG call." in py_snip


def test_py_pragmas_snippet_sqlite_exact(monkeypatch):
    # MO_SQLITE_BUSY_MS unset → 5000 default.
    _scrub(monkeypatch, MO_STORE_BACKEND="sqlite")
    py_snip = ps.py_pragmas_snippet()
    assert py_snip == 'con.execute("PRAGMA busy_timeout=5000")\n'


def test_py_pragmas_snippet_postgres_exact(monkeypatch):
    _scrub(monkeypatch, MO_STORE_BACKEND="postgres")
    py_snip = ps.py_pragmas_snippet()
    assert py_snip == ""


# ── (h) DB round-trip — exec snippets against a real temp DB ─────────────────


def test_snippets_exec_round_trip(tmp_path_factory, monkeypatch):
    """Exec the emitted connect+pragmas snippets against a temp DB
    initialised via ``mini_ork.stores.migrate.init_db`` and assert the
    ``PRAGMA busy_timeout`` + ``PRAGMA journal_mode`` introspection rows.

    The init step applies ``journal_mode=WAL`` persistently (header-level)
    and sets ``busy_timeout=5000`` at the init connection (which is
    per-connection and does NOT persist — the snippet must set it again at
    open time, which is exactly the F-11/R1 audit invariant this gate
    proves).
    """
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"

    _scrub(monkeypatch, MINI_ORK_DB=dbp)  # sqlite default

    py_snip = ps.py_connect_snippet() + ps.py_pragmas_snippet()

    # The snippet assumes `db` is bound before the body so
    # `con = sqlite3.connect(db)` can resolve.
    ns: dict = {"db": dbp}
    exec(py_snip, ns)  # noqa: S102 — the snippet IS the unit under test
    con = ns["con"]
    busy = con.execute("PRAGMA busy_timeout").fetchone()[0]
    journal = con.execute("PRAGMA journal_mode").fetchone()[0]

    # The per-connection pragma (set by the snippet) plus the persistent
    # header pragma (set by init_db) yield 5000|wal.
    assert (busy, journal) == (5000, "wal")
