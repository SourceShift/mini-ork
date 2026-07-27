"""Standalone unit tests for ``mini_ork.cli.inject``.

Replaces the bash-parity gate (against ``bin/mini-ork-inject``) as part of
the bash→Python migration: the Python CLI is now the sole implementation,
so its coverage no longer invokes the LIVE bash wrapper as a subprocess —
it asserts the port's behaviour directly. The expected values below are
the semantic contract the bash side used to pin (row round-trips, the
``--source operator-cli`` default injection, the ``--role`` →
``--role-target`` surface, exit codes + stderr text, float precision,
ttl propagation), now asserted on the port's output.

Seven cases:
  (a) happy-path emit — inserted row matches the CLI args
  (b) default-source injection — 'operator-cli' stamped when --source absent
  (c) --role surface — row sees role_target; invalid role exits 2
  (d) --message required — stderr text
  (e) DB missing — exit 1 with 'state.db not found'
  (f) float confidence precision — 0.123456789 round-trips within 1e-6
  (g) ttl_secs propagation — custom 7200 → expires_at is exactly 7200*1000
      ms after created_at

Environment isolation: monkeypatch ``MINI_ORK_DB`` / ``MINI_ORK_HOME`` so
the in-process Python port lands on the per-test temp DB (not the main
repo's state.db). The DB is seeded via
``mini_ork.stores.migrate.init_db``.
"""
from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import inject as py_cli  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402

# id/created_at/expires_at are stripped where timing-dependent.
_STRIP_KEYS = ("id", "created_at", "expires_at")


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
def _init_db(tmp_path_factory, *, name: str) -> tuple[str, str]:
    """Spin up a fresh mini-ork SQLite DB via ``init_db``.

    Returns ``(db_path, home_dir)``. ``tmp_path_factory.mktemp(name)``
    guarantees a unique sub-directory per call, so two DBs in the same
    test don't collide.
    """
    home = tmp_path_factory.mktemp(name)
    dbp = str(home / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    return dbp, str(home)


def _point_python_env(monkeypatch: pytest.MonkeyPatch, db: str, home: str) -> None:
    """Redirect the Python process's ``_resolve_db`` to the temp db."""
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", home)


def _row_from_tuple(row: tuple) -> dict:
    return {
        "id": row[0],
        "run_id": row[1],
        "role_target": row[2],
        "severity": row[3],
        "message": row[4],
        "source": row[5],
        "confidence": row[6],
        "created_at": row[7],
        "expires_at": row[8],
    }


def _only_row(db: str) -> dict:
    """Read the single operator_steering row in a fresh test DB."""
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            """SELECT id, run_id, role_target, severity, message, source,
                      confidence, created_at, expires_at
                 FROM operator_steering"""
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    return _row_from_tuple(rows[0])


def _strip(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in _STRIP_KEYS}


def _run_with_captured_stderr(argv: list[str]) -> str:
    """Run py_cli.main() with stderr redirected to a StringIO buffer.

    The CLI writes to ``sys.stderr`` directly so pytest's capsys sees
    nothing. We swap in a buffer to assert the textual output.
    """
    buf = io.StringIO()
    real = sys.stderr
    sys.stderr = buf
    try:
        py_cli.main(argv)
    finally:
        sys.stderr = real
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# (a) happy-path emit — inserted row matches the CLI args
# ─────────────────────────────────────────────────────────────────────────────
def test_happy_path_emit(tmp_path_factory, monkeypatch):
    """The CLI inserts an ``operator_steering`` row whose content (run_id /
    role_target / severity / message / source / confidence) matches the
    inputs."""
    db, home = _init_db(tmp_path_factory, name="a")
    _point_python_env(monkeypatch, db, home)

    rc = py_cli.main([
        "--run-id", "r-a",
        "--role", "implementer",
        "--message", "from-py",
        "--severity", "warn",
        "--source", "py-src",
        "--confidence", "0.7",
    ])
    assert rc == 0, f"py inject returned {rc}"

    py_row = _only_row(db)
    assert _strip(py_row) == {
        "run_id": "r-a",
        "role_target": "implementer",
        "severity": "warn",
        "message": "from-py",
        "source": "py-src",
        "confidence": 0.7,
    }


# ─────────────────────────────────────────────────────────────────────────────
# (b) default-source injection — 'operator-cli' stamped when --source absent
# ─────────────────────────────────────────────────────────────────────────────
def test_default_source_injection(tmp_path_factory, monkeypatch):
    """The CLI adds ``--source operator-cli`` when the user omits
    ``--source``. An argparse ``default='operator-cli'`` is *not* a valid
    mirror of the old wrapper because it would override an explicit
    empty-string ``--source`` — the port must inject the default itself."""
    db, home = _init_db(tmp_path_factory, name="b")
    _point_python_env(monkeypatch, db, home)

    rc = py_cli.main([
        "--run-id", "r-b",
        "--role", "reviewer",
        "--message", "from-py",
    ])
    assert rc == 0

    py_row = _only_row(db)
    assert py_row["source"] == "operator-cli"


# ─────────────────────────────────────────────────────────────────────────────
# (c) --role surface — row sees role_target; invalid role exits 2
# ─────────────────────────────────────────────────────────────────────────────
def test_role_surface(tmp_path_factory, monkeypatch):
    """The CLI inserts a row with ``role_target='reviewer'`` when the user
    passes ``--role reviewer``. An invalid role is rejected by argparse
    ``choices=`` which fires BEFORE emit() is reached and exits 2 with
    "invalid choice: 'BOGUS'"."""
    db, home = _init_db(tmp_path_factory, name="c")
    _point_python_env(monkeypatch, db, home)

    rc = py_cli.main([
        "--run-id", "r-c",
        "--role", "reviewer",
        "--message", "from-py",
    ])
    assert rc == 0

    py_row = _only_row(db)
    assert py_row["role_target"] == "reviewer"

    real_stderr = sys.stderr
    captured = io.StringIO()
    sys.stderr = captured
    try:
        rc_bad = py_cli.main([
            "--run-id", "r-c",
            "--role", "BOGUS",
            "--message", "x",
        ])
    finally:
        sys.stderr = real_stderr
    assert rc_bad == 2
    py_err = captured.getvalue()
    assert "invalid choice" in py_err and "BOGUS" in py_err


# ─────────────────────────────────────────────────────────────────────────────
# (d) --message required — stderr text
# ─────────────────────────────────────────────────────────────────────────────
def test_message_required_exits_2(tmp_path_factory, monkeypatch):
    """Omitting ``--message`` → main()'s explicit check writes
    ``--message required`` to stderr."""
    db, home = _init_db(tmp_path_factory, name="d")
    _point_python_env(monkeypatch, db, home)

    py_stderr = _run_with_captured_stderr(
        ["--run-id", "r-d", "--role", "planner"]  # no --message
    )
    assert "--message required" in py_stderr


# ─────────────────────────────────────────────────────────────────────────────
# (e) DB missing — exit 1 with 'state.db not found'
# ─────────────────────────────────────────────────────────────────────────────
def test_db_missing_exits_1(tmp_path_factory, monkeypatch):
    """``state.db not found: …`` surfaces as exit 1. We point MINI_ORK_DB
    at a path that does NOT exist."""
    # Use a path that won't be created. tmp_path_factory still gives us
    # a unique temp dir but we deliberately do NOT initialize a DB there.
    home = tmp_path_factory.mktemp("e")
    dbp = str(home / "state.db")  # NOT created — init_db not run.
    _point_python_env(monkeypatch, dbp, str(home))

    rc = py_cli.main([
        "--run-id", "r-e",
        "--role", "planner",
        "--message", "x",
    ])
    assert rc == 1
    py_stderr = _run_with_captured_stderr(
        ["--run-id", "r-e", "--role", "planner", "--message", "x"]
    )
    assert "state.db not found" in py_stderr


# ─────────────────────────────────────────────────────────────────────────────
# (f) float confidence precision — 0.123456789 round-trips within 1e-6
# ─────────────────────────────────────────────────────────────────────────────
def test_float_confidence_precision(tmp_path_factory, monkeypatch):
    """Confidence 0.123456789 must round-trip within 1e-6. SQLite REAL is
    IEEE 754 double; argparse parses the literal identically."""
    db, home = _init_db(tmp_path_factory, name="f")
    _point_python_env(monkeypatch, db, home)

    rc = py_cli.main([
        "--run-id", "r-f",
        "--role", "planner",
        "--message", "from-py",
        "--confidence", "0.123456789",
    ])
    assert rc == 0

    py_row = _only_row(db)
    assert abs(py_row["confidence"] - 0.123456789) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (g) ttl_secs propagation — custom 7200 → expires_at is exactly 7200*1000
#     ms after created_at.
# ─────────────────────────────────────────────────────────────────────────────
def test_ttl_secs_propagation(tmp_path_factory, monkeypatch):
    """Custom ``--ttl-secs 7200`` must produce expires_at == created_at +
    7,200,000. The *delta* must match the requested ttl exactly."""
    db, home = _init_db(tmp_path_factory, name="g")
    _point_python_env(monkeypatch, db, home)

    rc = py_cli.main([
        "--run-id", "r-g",
        "--role", "planner",
        "--message", "from-py",
        "--ttl-secs", "7200",
    ])
    assert rc == 0

    py_row = _only_row(db)
    py_delta = int(py_row["expires_at"]) - int(py_row["created_at"])
    assert py_delta == 7_200_000, (
        f"ttl delta: {py_delta} (expected 7,200,000 ms = 7200 s)"
    )
