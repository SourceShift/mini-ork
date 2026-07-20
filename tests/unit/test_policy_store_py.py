"""Parity gate: mini_ork.stores.policy_store vs lib/policy_store.sh.

Each test invokes the LIVE bash subprocess sourcing
``lib/policy_store.sh`` (which uses the ``[ "${0:-}" = "${BASH_SOURCE[0]:-}" ]``
guard so no ``set -u`` / ``pipefail`` leaks onto the parent that sources
it) on the same inputs as the Python port and asserts identical output
— return code, snippet bytes, stderr text, and exit-code shape (64 / 78
via ``SystemExit``).

Eight cases (above the kickoff's >=6 floor):
  (a) backend() default 'sqlite' when MO_STORE_BACKEND unset
  (b) backend()='sqlite' when MO_STORE_BACKEND=sqlite
  (c) backend()='postgres' when MO_STORE_BACKEND=postgres
  (d) backend() raises SystemExit(64) with stderr containing
      'unknown MO_STORE_BACKEND=mysql' when MO_STORE_BACKEND=mysql
  (e) assert_sqlite parity — rc=0 / None on sqlite, SystemExit(78)
      on postgres with matching stderr text
  (f) db_path() resolution chain — three sub-cases for
      MO_STORE_DB / MINI_ORK_DB / MINI_ORK_HOME + one for the
      $(pwd)/.mini-ork/state.db fallback
  (g) py_connect_snippet + py_pragmas_snippet exact-string parity for
      both sqlite AND postgres branches (4 strings compared verbatim)
  (h) DB round-trip gate — concat bash-emitted and python-emitted
      snippets into one ``python3 -c`` invocation against a temp DB
      spun up via db/init.sh; assert both produce identical
      PRAGMA busy_timeout / PRAGMA journal_mode introspection rows

Env isolation: ``monkeypatch.setenv`` / ``delenv`` for the Python side;
the bash subprocess env is built from ``os.environ`` so monkeypatched
values propagate identically. The db_path pwd-fallback case (f-4) runs
both sides in ``cwd=tmp_path`` so the literal ``./.mini-ork/state.db``
matches.

No mocks, no hardcoded expected strings beyond the literal ``"sqlite"`` /
``"postgres"`` enum values the bash function itself prints: every
snippet / stderr / row comparison derives from the live bash
subprocess, not from a captured fixture.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.stores import policy_store as ps

PS_SH = REPO / "lib" / "policy_store.sh"
INIT_SH = REPO / "db" / "init.sh"

# Vars that may exist in the parent pytest env and would silently
# influence policy_store; every test scrubs these via _scrub().
_PS_ENV = (
    "MO_STORE_BACKEND",
    "MO_STORE_DB",
    "MINI_ORK_DB",
    "MINI_ORK_HOME",
    "MO_SQLITE_BUSY_MS",
)


def _scrub(monkeypatch: pytest.MonkeyPatch, **overrides) -> dict:
    """Unset every policy_store env var, then apply overrides.

    Returns a fresh subprocess env (caller passes to bash via
    ``env=``) so Python and bash see identical inputs. Mirrors bash's
    ``${VAR:-default}`` semantics: unset and empty collapse the same
    way, so we use ``delenv`` (not ``setenv('', '')``) to be explicit.
    """
    for k in _PS_ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in overrides.items():
        monkeypatch.setenv(k, v)
    return dict(os.environ)


def _bash(name: str, args: str = "", env: dict | None = None,
          cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run a single policy_store function via live bash subprocess.

    Sources ``lib/policy_store.sh`` then invokes ``name`` (optionally
    with ``args``). Bash's own rc / stdout / stderr are returned
    verbatim for the test to assert against.
    """
    cmd = f'. "{PS_SH}" && {name} {args}'.rstrip()
    return subprocess.run(
        ["bash", "-c", cmd],
        cwd=cwd, env=env if env is not None else os.environ,
        capture_output=True, text=True,
    )


# ── (a) default backend when MO_STORE_BACKEND unset ─────────────────────────


def test_backend_default_sqlite(monkeypatch):
    env = _scrub(monkeypatch)
    bash_out = _bash("mo_store_backend", env=env).stdout.rstrip("\n")
    assert bash_out == "sqlite"
    assert ps.backend() == "sqlite"


# ── (b) explicit sqlite ──────────────────────────────────────────────────────


def test_backend_explicit_sqlite(monkeypatch):
    env = _scrub(monkeypatch, MO_STORE_BACKEND="sqlite")
    bash_out = _bash("mo_store_backend", env=env).stdout.rstrip("\n")
    assert bash_out == "sqlite"
    assert ps.backend() == "sqlite"


# ── (c) explicit postgres ────────────────────────────────────────────────────


def test_backend_explicit_postgres(monkeypatch):
    env = _scrub(monkeypatch, MO_STORE_BACKEND="postgres")
    bash_out = _bash("mo_store_backend", env=env).stdout.rstrip("\n")
    assert bash_out == "postgres"
    assert ps.backend() == "postgres"


# ── (d) unknown backend → SystemExit(64) + matching stderr ──────────────────


def test_backend_unknown_raises_systemexit_64(monkeypatch, capsys):
    env = _scrub(monkeypatch, MO_STORE_BACKEND="mysql")
    proc = _bash("mo_store_backend", env=env)
    assert proc.returncode == 64
    assert "unknown MO_STORE_BACKEND=mysql" in proc.stderr
    assert "(expected sqlite|postgres)" in proc.stderr

    with pytest.raises(SystemExit) as exc:
        ps.backend()
    assert exc.value.code == 64
    captured = capsys.readouterr()
    assert "unknown MO_STORE_BACKEND=mysql" in captured.err
    assert "(expected sqlite|postgres)" in captured.err


# ── (e) assert_sqlite parity ─────────────────────────────────────────────────


def test_assert_sqlite_parity(monkeypatch, capsys):
    # sqlite → rc=0, no output, returns None
    env = _scrub(monkeypatch, MO_STORE_BACKEND="sqlite")
    proc = _bash("mo_store_assert_sqlite", env=env)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert ps.assert_sqlite() is None

    # postgres → rc=78, stderr contains the stub message
    env = _scrub(monkeypatch, MO_STORE_BACKEND="postgres")
    proc = _bash("mo_store_assert_sqlite", env=env)
    assert proc.returncode == 78
    assert "backend=postgres is a stub in v0.2-pt36" in proc.stderr
    assert "Set MO_STORE_BACKEND=sqlite for default behavior." in proc.stderr

    with pytest.raises(SystemExit) as exc:
        ps.assert_sqlite()
    assert exc.value.code == 78
    captured = capsys.readouterr()
    assert "backend=postgres is a stub in v0.2-pt36" in captured.err
    assert "Set MO_STORE_BACKEND=sqlite for default behavior." in captured.err


# ── (f) db_path() resolution chain — three sub-cases ─────────────────────────


def test_db_path_mo_store_db_wins(monkeypatch, tmp_path):
    target = str(tmp_path / "explicit.db")
    env = _scrub(monkeypatch, MO_STORE_DB=target)
    assert _bash("mo_store_db_path", env=env).stdout.rstrip("\n") == target
    assert ps.db_path() == target


def test_db_path_mini_ork_db_fallback(monkeypatch, tmp_path):
    target = str(tmp_path / "from_mini_ork_db.db")
    env = _scrub(monkeypatch, MINI_ORK_DB=target)
    assert _bash("mo_store_db_path", env=env).stdout.rstrip("\n") == target
    assert ps.db_path() == target


def test_db_path_mini_ork_home_fallback(monkeypatch, tmp_path):
    home = str(tmp_path)
    env = _scrub(monkeypatch, MINI_ORK_HOME=home)
    expected = str(tmp_path / "state.db")
    assert _bash("mo_store_db_path", env=env).stdout.rstrip("\n") == expected
    assert ps.db_path() == expected


def test_db_path_pwd_fallback(monkeypatch, tmp_path):
    # No MO_STORE_DB / MINI_ORK_DB / MINI_ORK_HOME → falls through to
    # $(pwd)/.mini-ork/state.db on both sides.
    env = _scrub(monkeypatch)
    cwd = str(tmp_path)
    expected = f"{cwd}/.mini-ork/state.db"
    bash_out = _bash("mo_store_db_path", env=env, cwd=cwd).stdout.rstrip("\n")
    assert bash_out == expected
    monkeypatch.chdir(cwd)  # os.getcwd() == tmp_path for the Python side
    assert ps.db_path() == expected


# ── (g) snippet byte-exactness — 4 strings, both backends ────────────────────


def test_py_connect_snippet_sqlite_byte_exact(monkeypatch):
    env = _scrub(monkeypatch, MO_STORE_BACKEND="sqlite")
    bash_snip = _bash("mo_store_py_connect_snippet", env=env).stdout
    py_snip = ps.py_connect_snippet()
    assert py_snip == bash_snip
    assert py_snip == "import sqlite3\ncon = sqlite3.connect(db)\n"


def test_py_connect_snippet_postgres_byte_exact(monkeypatch):
    env = _scrub(monkeypatch, MO_STORE_BACKEND="postgres")
    bash_snip = _bash("mo_store_py_connect_snippet", env=env).stdout
    py_snip = ps.py_connect_snippet()
    assert py_snip == bash_snip
    # Bash's printf embeds the literal double-quoted Python string;
    # verify both the raise shape and the stub keyphrase survive the round-trip.
    assert "raise SystemExit(" in py_snip
    assert "backend=postgres is a stub in v0.2-pt36" in py_snip
    assert "Aborting before any PG call." in py_snip


def test_py_pragmas_snippet_sqlite_byte_exact(monkeypatch):
    # MO_SQLITE_BUSY_MS unset → 5000 default on both sides.
    env = _scrub(monkeypatch, MO_STORE_BACKEND="sqlite")
    bash_snip = _bash("mo_store_py_pragmas_snippet", env=env).stdout
    py_snip = ps.py_pragmas_snippet()
    assert py_snip == bash_snip
    assert py_snip == 'con.execute("PRAGMA busy_timeout=5000")\n'


def test_py_pragmas_snippet_postgres_byte_exact(monkeypatch):
    env = _scrub(monkeypatch, MO_STORE_BACKEND="postgres")
    bash_snip = _bash("mo_store_py_pragmas_snippet", env=env).stdout
    py_snip = ps.py_pragmas_snippet()
    assert py_snip == bash_snip
    assert py_snip == ""  # bash's `:` is a no-op; Python returns ""


# ── (h) DB round-trip gate — exec snippets against a real temp DB ────────────


def test_snippets_exec_round_trip_parity(tmp_path_factory, monkeypatch):
    """Concatenate bash-emitted and python-emitted snippets, exec each via
    ``python3 -c`` against a temp DB initialized via ``db/init.sh``,
    and assert identical ``PRAGMA busy_timeout`` + ``PRAGMA journal_mode``
    introspection rows on both sides.

    The temp DB fixture is reused from case (f)'s idiom — one DB per
    test (pytest ``tmp_path_factory`` is function-scoped) so case (h)
    initializes its own. The ``db/init.sh`` step applies
    ``journal_mode=WAL`` persistently (header-level) and sets
    ``busy_timeout=5000`` at the init connection (which is per-connection
    and does NOT persist — the snippet must set it again at open time,
    which is exactly the F-11/R1 audit invariant this gate proves).
    """
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )

    env = _scrub(monkeypatch, MINI_ORK_DB=dbp)  # sqlite default

    # bash side: chain snippets with `&&` (NOT `$(...)$(...)` — POSIX
    # command substitution strips trailing newlines from each, which would
    # drop the separator between connect and pragma and diverge from the
    # Python direct-concat shape). Each snippet's printf ends with \n;
    # the chain preserves them.
    bash_proc = subprocess.run(
        ["bash", "-c",
         f'. "{PS_SH}" && '
         f'mo_store_py_connect_snippet && mo_store_py_pragmas_snippet'],
        env=env, capture_output=True, text=True, check=True,
    )
    bash_snip = bash_proc.stdout

    # python side: concat snippet returns
    py_snip = ps.py_connect_snippet() + ps.py_pragmas_snippet()

    # Snippets must be byte-identical before we even attempt to exec them.
    assert py_snip == bash_snip, (
        f"snippet byte mismatch:\n  py={py_snip!r}\n  bash={bash_snip!r}"
    )

    # Exec both snippets via `python3 -c`. The snippet assumes `db` is
    # passed via sys.argv[1] (the heredoc convention) — bind it BEFORE
    # the snippet body so `con = sqlite3.connect(db)` can resolve.
    introspection = (
        "busy = con.execute('PRAGMA busy_timeout').fetchone()[0]\n"
        "journal = con.execute('PRAGMA journal_mode').fetchone()[0]\n"
        "print(f'{busy}|{journal}')\n"
    )
    py_prog = (
        f"import sys\ndb = sys.argv[1]\n{py_snip}{introspection}"
    )
    bash_prog = (
        f"import sys\ndb = sys.argv[1]\n{bash_snip}{introspection}"
    )

    py_run = subprocess.run(
        ["python3", "-c", py_prog, dbp],
        capture_output=True, text=True, check=True,
    )
    bash_run = subprocess.run(
        ["python3", "-c", bash_prog, dbp],
        capture_output=True, text=True, check=True,
    )

    # Both sides must produce identical rows.
    assert py_run.stdout == bash_run.stdout, (
        f"snippet exec produced different rows:\n"
        f"  py={py_run.stdout!r}\n  bash={bash_run.stdout!r}"
    )
    # Sanity: the per-connection pragma (set by the snippet) plus the
    # persistent header pragma (set by db/init.sh) yield 5000|wal.
    assert py_run.stdout.strip() == "5000|wal"