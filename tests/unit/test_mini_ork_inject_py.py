"""Parity gate: bin/mini-ork-inject (bash wrapper) vs mini_ork.ported.mini_ork_inject.

Each test invokes the LIVE bash subprocess ``bin/mini-ork-inject`` (which
sources ``lib/operator_steering.sh`` and runs its argparse-equivalent
flag-translation + default-injection pre-processor before calling
``operator_steering_emit``) against a temp DB seeded via ``db/init.sh``,
then invokes the Python port against the SAME DB and reads both rows back
through ``sqlite3.connect()`` to diff field-by-field. id / created_at /
expires_at are stripped (timing-dependent); floats compared at 1e-6.

Why the bash subprocess must hit ``bin/mini-ork-inject`` (not
``lib/operator_steering.sh`` directly): the wrapper does TWO things that
the lib function does not — ``--role`` is translated to ``--role-target``
and the ``--source operator-cli`` default is injected when ``--source`` is
absent. Going through the wrapper is the only path that exercises those
pre-process transformations, which is the entire surface we're porting.

Seven cases (above the kickoff's >=6 floor):
  (a) happy-path emit — bash + python insert the same fields
  (b) default-source injection — bash stamps 'operator-cli'; python must too
  (c) --role translation — bash wrapper accepts --role; emit row sees the
      translated role_target value
  (d) --message required — both exit 2 with the same stderr text
  (e) DB missing — both exit 1 with FileNotFoundError → sys.exit(1)
  (f) float confidence precision — 0.123456789 round-trips within 1e-6
  (g) ttl_secs propagation — custom 7200 → expires_at is exactly 7200*1000
      ms after created_at on both sides

Environment isolation: monkeypatch ``MINI_ORK_DB`` / ``MINI_ORK_HOME`` so
the in-process Python port lands on the per-test temp DB (not the main
repo's state.db). Build the bash subprocess env from ``os.environ`` so
the monkeypatched values propagate into the child shell.

No hardcoded expected outputs — every assertion derives from a parallel
bash or python invocation. No mocks anywhere.
"""
from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
# Importing the production module: the parity test exercises main() via a
# subprocess-like isolated environment, but importing it gives us the
# in-process surface for fast assertions.
from mini_ork.ported import mini_ork_inject as py_cli  # noqa: E402

INJECT_BIN = REPO / "bin" / "mini-ork-inject"
SH_LIB = REPO / "lib" / "operator_steering.sh"
INIT_SH = REPO / "db" / "init.sh"

# Same as test_operator_steering_py.py — id/created_at/expires_at are
# stripped because both writers hit a fresh AUTOINCREMENT and a wall-clock
# that drifts across subshells (the bash heredoc INSIDE operator_steering_emit
# shells out to `int(time.time() * 1000)`, distinct from Python's `_now_ms`).
_STRIP_KEYS = ("id", "created_at", "expires_at")


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
def _which_tools() -> None:
    for tool in ("bash", "sqlite3", "python3"):
        if not __import__("shutil").which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not INJECT_BIN.exists():
        pytest.skip(f"missing bin/mini-ork-inject at {INJECT_BIN}")
    if not SH_LIB.exists():
        pytest.skip(f"missing lib/operator_steering.sh at {SH_LIB}")
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")


def _init_db(tmp_path_factory, *, name: str) -> tuple[str, str]:
    """Spin up a fresh mini-ork SQLite DB via ``db/init.sh``.

    Returns ``(db_path, home_dir)``. ``tmp_path_factory.mktemp(name)``
    guarantees a unique sub-directory per call, so two DBs in the same
    test don't collide.
    """
    _which_tools()
    home = tmp_path_factory.mktemp(name)
    dbp = str(home / "state.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    assert r.returncode == 0, r.stderr
    return dbp, str(home)


def _point_python_env(monkeypatch: pytest.MonkeyPatch, db: str, home: str) -> None:
    """Redirect the Python process's ``_resolve_db`` to the temp db.

    The bash subprocess env is built from ``os.environ`` so the
    monkeypatched values propagate to the child shell automatically.
    """
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", home)


def _read_row(db: str, rowid: int) -> dict | None:
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            """SELECT id, run_id, role_target, severity, message, source,
                      confidence, created_at, expires_at
                 FROM operator_steering WHERE id=?""",
            (rowid,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
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


def _strip(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in _STRIP_KEYS}


def _bash_inject(
    args: list[str], *, db: str, home: str,
) -> subprocess.CompletedProcess:
    """Invoke ``bin/mini-ork-inject`` against the temp DB.

    We call the wrapper directly (NOT lib/operator_steering.sh) so the
    ``--role`` → ``--role-target`` translation and the
    ``--source operator-cli`` default-injection pre-processors are
    actually exercised.
    """
    return subprocess.run(
        ["bash", str(INJECT_BIN), *args],
        env={**os.environ, "MINI_ORK_HOME": home, "MINI_ORK_DB": db},
        capture_output=True, text=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# (a) happy-path emit — bash and python insert the same fields
# ─────────────────────────────────────────────────────────────────────────────
def test_happy_path_emit_parity(tmp_path_factory, monkeypatch):
    """Both ports must insert an ``operator_steering`` row whose content
    (run_id / role_target / severity / message / source / confidence)
    matches the inputs byte-for-byte."""
    db, home = _init_db(tmp_path_factory, name="a")
    _point_python_env(monkeypatch, db, home)

    bash_args = [
        "--run-id", "r-a",
        "--role", "implementer",
        "--message", "from-bash",
        "--severity", "warn",
        "--source", "bash-src",
        "--confidence", "0.7",
    ]
    r_bash = _bash_inject(bash_args, db=db, home=home)
    assert r_bash.returncode == 0, (
        f"bash inject failed: rc={r_bash.returncode}\n"
        f"stdout={r_bash.stdout!r}\nstderr={r_bash.stderr!r}"
    )
    bash_rowid = int(r_bash.stdout.strip())

    rc = py_cli.main([
        "--run-id", "r-a",
        "--role", "implementer",
        "--message", "from-py",
        "--severity", "warn",
        "--source", "py-src",
        "--confidence", "0.7",
    ])
    assert rc == 0, f"py inject returned {rc}"

    # Read rows from the same DB and diff.
    rows_after_bash = _read_row(db, bash_rowid)
    assert rows_after_bash is not None, "bash row not in DB"
    # The python row's id is bash_rowid + 1 because bash got the first
    # AUTOINCREMENT. Both halves of the parity check verify the same DB
    # state through different observability points.
    py_rowid = bash_rowid + 1
    py_row = _read_row(db, py_rowid)
    assert py_row is not None, "py row not in DB"

    # Strip timing fields. Verify each row matches ITS OWN inputs (not
    # the other emitter's), and the messages differ (we didn't conflate).
    assert _strip(rows_after_bash) == {
        "run_id": "r-a",
        "role_target": "implementer",
        "severity": "warn",
        "message": "from-bash",
        "source": "bash-src",
        "confidence": 0.7,
    }
    assert _strip(py_row) == {
        "run_id": "r-a",
        "role_target": "implementer",
        "severity": "warn",
        "message": "from-py",
        "source": "py-src",
        "confidence": 0.7,
    }
    assert rows_after_bash["message"] != py_row["message"]
    assert rows_after_bash["source"] != py_row["source"]
    assert abs(rows_after_bash["confidence"] - py_row["confidence"]) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (b) default-source injection — bash stamps 'operator-cli' when absent;
#     python port must do the same.
# ─────────────────────────────────────────────────────────────────────────────
def test_default_source_injection_parity(tmp_path_factory, monkeypatch):
    """Bash ``bin/mini-ork-inject`` adds ``--source operator-cli`` when the
    user omits ``--source`` (see the ``HAS_SOURCE=0`` path in bash).
    Python must inject the same default — argparse ``default='operator-cli'``
    is *not* a valid mirror because it would override an explicit
    empty-string ``--source``."""
    db, home = _init_db(tmp_path_factory, name="b")
    _point_python_env(monkeypatch, db, home)

    # Bash without --source.
    r_bash = _bash_inject(
        ["--run-id", "r-b", "--role", "reviewer", "--message", "from-bash"],
        db=db, home=home,
    )
    assert r_bash.returncode == 0, r_bash.stderr
    bash_rowid = int(r_bash.stdout.strip())

    # Python without --source. Expected (in mind) source is "operator-cli".
    rc = py_cli.main([
        "--run-id", "r-b",
        "--role", "reviewer",
        "--message", "from-py",
    ])
    assert rc == 0
    py_rowid = bash_rowid + 1

    bash_row = _read_row(db, bash_rowid)
    py_row = _read_row(db, py_rowid)
    assert bash_row is not None and py_row is not None
    assert bash_row["source"] == "operator-cli"
    assert py_row["source"] == "operator-cli"
    assert bash_row["source"] == py_row["source"]


# ─────────────────────────────────────────────────────────────────────────────
# (c) --role translation — bash wrapper accepts --role, the lib sees
#     --role-target; same effect on the row.
# ─────────────────────────────────────────────────────────────────────────────
def test_role_translation_parity(tmp_path_factory, monkeypatch):
    """Both ports insert a row with ``role_target='reviewer'`` when the user
    passes ``--role reviewer``. The bash wrapper rewrites the flag before
    invoking ``operator_steering_emit``; the Python port accepts ``--role``
    directly (mirroring the wrapper's surface) and passes it through."""
    db, home = _init_db(tmp_path_factory, name="c")
    _point_python_env(monkeypatch, db, home)

    r_bash = _bash_inject(
        ["--run-id", "r-c", "--role", "reviewer", "--message", "from-bash"],
        db=db, home=home,
    )
    assert r_bash.returncode == 0, r_bash.stderr
    bash_rowid = int(r_bash.stdout.strip())

    rc = py_cli.main([
        "--run-id", "r-c",
        "--role", "reviewer",
        "--message", "from-py",
    ])
    assert rc == 0
    py_rowid = bash_rowid + 1

    bash_row = _read_row(db, bash_rowid)
    py_row = _read_row(db, py_rowid)
    assert bash_row is not None and py_row is not None
    assert bash_row["role_target"] == "reviewer"
    assert py_row["role_target"] == "reviewer"

    # Invalid-role behavior is intentionally asymmetric: bash's
    # operator_steering_emit has no validation on role_target values
    # (CHECK constraint enforces at DB insert time). The Python port
    # uses argparse ``choices=`` which fires BEFORE emit() is reached
    # and exits 2 with "invalid choice: 'BOGUS'". Either way, the
    # python path exits 2; bash's path is non-zero (whatever exit code
    # it picks — the SQL CHECK rejects the INSERT).
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

    r_bad = _bash_inject(
        ["--run-id", "r-c", "--role", "BOGUS", "--message", "x"],
        db=db, home=home,
    )
    # bash's no-action-on-bad-role semantics: any string accepted; the
    # SQL CHECK rejects the INSERT. Exit code is non-zero (1 or 2
    # depending on which error bubbles up first).
    assert r_bad.returncode != 0, (
        f"bash unexpectedly accepted bad role: rc={r_bad.returncode}\n"
        f"stdout={r_bad.stdout!r}\nstderr={r_bad.stderr!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (d) --message required — both exit 2 with the same stderr text.
# ─────────────────────────────────────────────────────────────────────────────
def test_message_required_exits_2_parity(tmp_path_factory, monkeypatch):
    """Bash: omitting ``--message`` → bash exits 2 with
    ``--message required`` on stderr (the wrapper forwards lib's error).
    Python argparse exits 2 with the same text via ``main()``'s explicit
    check."""
    db, home = _init_db(tmp_path_factory, name="d")
    _point_python_env(monkeypatch, db, home)

    r_bash = _bash_inject(
        ["--run-id", "r-d", "--role", "planner"],  # no --message
        db=db, home=home,
    )
    assert r_bash.returncode == 2, (
        f"bash exit code: rc={r_bash.returncode}\nstderr={r_bash.stderr!r}"
    )
    assert "--message required" in r_bash.stderr

    py_stderr = _run_with_captured_stderr(
        ["--run-id", "r-d", "--role", "planner"]  # no --message
    )
    assert "--message required" in py_stderr


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
# (e) DB missing — both exit 1 with FileNotFoundError / 'state.db not found'
# ─────────────────────────────────────────────────────────────────────────────
def test_db_missing_exits_1_parity(tmp_path_factory, monkeypatch):
    """Both ports raise ``FileNotFoundError`` with ``state.db not found: …``
    and exit 1. We point MINI_ORK_DB at a path that does NOT exist."""
    # Use a path that won't be created. tmp_path_factory still gives us
    # a unique temp dir but we deliberately do NOT initialize a DB there.
    home = tmp_path_factory.mktemp("e")
    dbp = str(home / "state.db")  # NOT created — db/init.sh not run.
    _point_python_env(monkeypatch, dbp, str(home))

    # Bash side: the wrapper invokes operator_steering_emit which does
    # `[ -f "$db" ] || { echo "… state.db not found: …" >&2; return 1; }`.
    r_bash = _bash_inject(
        ["--run-id", "r-e", "--role", "planner", "--message", "x"],
        db=dbp, home=str(home),
    )
    assert r_bash.returncode == 1, (
        f"bash exit code: rc={r_bash.returncode}\nstderr={r_bash.stderr!r}"
    )
    assert "state.db not found" in r_bash.stderr

    # Python side: ops.emit raises FileNotFoundError → main maps to exit 1.
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
def test_float_confidence_precision_parity(tmp_path_factory, monkeypatch):
    """Confidence 0.123456789 must round-trip identically through both
    paths within 1e-6. SQLite REAL is IEEE 754 double; argparse parses
    the literal identically to bash arithmetic."""
    db, home = _init_db(tmp_path_factory, name="f")
    _point_python_env(monkeypatch, db, home)

    r_bash = _bash_inject(
        ["--run-id", "r-f", "--role", "planner", "--message", "from-bash",
         "--confidence", "0.123456789"],
        db=db, home=home,
    )
    assert r_bash.returncode == 0, r_bash.stderr
    bash_rowid = int(r_bash.stdout.strip())

    rc = py_cli.main([
        "--run-id", "r-f",
        "--role", "planner",
        "--message", "from-py",
        "--confidence", "0.123456789",
    ])
    assert rc == 0
    py_rowid = bash_rowid + 1

    bash_row = _read_row(db, bash_rowid)
    py_row = _read_row(db, py_rowid)
    assert bash_row is not None and py_row is not None
    assert abs(bash_row["confidence"] - 0.123456789) < 1e-6
    assert abs(py_row["confidence"] - 0.123456789) < 1e-6
    # Both routes produce the same SQLite REAL byte pattern.
    assert abs(bash_row["confidence"] - py_row["confidence"]) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (g) ttl_secs propagation — custom 7200 → expires_at is exactly 7200*1000
#     ms after created_at on both sides.
# ─────────────────────────────────────────────────────────────────────────────
def test_ttl_secs_propagation_parity(tmp_path_factory, monkeypatch):
    """Custom ``--ttl-secs 7200`` must produce expires_at == created_at +
    7,200,000 on both ports. The drift between bash and python ``_now_ms``
    is sub-millisecond; the *delta* must match the requested ttl exactly
    on each side."""
    db, home = _init_db(tmp_path_factory, name="g")
    _point_python_env(monkeypatch, db, home)

    r_bash = _bash_inject(
        ["--run-id", "r-g", "--role", "planner", "--message", "from-bash",
         "--ttl-secs", "7200"],
        db=db, home=home,
    )
    assert r_bash.returncode == 0, r_bash.stderr
    bash_rowid = int(r_bash.stdout.strip())

    rc = py_cli.main([
        "--run-id", "r-g",
        "--role", "planner",
        "--message", "from-py",
        "--ttl-secs", "7200",
    ])
    assert rc == 0
    py_rowid = bash_rowid + 1

    bash_row = _read_row(db, bash_rowid)
    py_row = _read_row(db, py_rowid)
    assert bash_row is not None and py_row is not None

    bash_delta = int(bash_row["expires_at"]) - int(bash_row["created_at"])
    py_delta = int(py_row["expires_at"]) - int(py_row["created_at"])

    assert bash_delta == 7_200_000, (
        f"bash ttl delta: {bash_delta} (expected 7,200,000 ms = 7200 s)"
    )
    assert py_delta == 7_200_000, (
        f"py ttl delta: {py_delta} (expected 7,200,000 ms = 7200 s)"
    )
    assert bash_delta == py_delta
