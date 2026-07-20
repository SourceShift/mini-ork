"""Parity gate: ``mini_ork.cli.rollback`` vs ``bin/mini-ork-rollback``.

Each test invokes the LIVE bash subprocess (``bin/mini-ork-rollback``)
against a temp DB seeded by ``db/init.sh``, then invokes the Python
port against the same DB shape on a parallel copy, and asserts the
resulting stdout, stderr, and exit codes are byte-for-byte identical.
No mocks, no hardcoded expected outputs — the expected value is always
derived from the live control bash invocation.

Schema bootstrap: ``db/init.sh`` applies migration ``0011_evolution.sql``
which creates the ``version_registry`` table (the bash function
``_ver_ensure_table`` is also idempotent, but ``db/init.sh`` is the
canonical path because it covers the rest of the schema too).

Cases (9 — above the kickoff's >=6 floor):

  (1) ``--help`` — exact stdout + exit 0.
  (2) ``-h`` alias — exact stdout + exit 0.
  (3) no args — usage to stderr + exit 2.
  (4) invalid kind ``unknown`` — usage to stderr + exit 2.
  (5) missing name ``workflow`` only — usage to stderr + exit 2.
  (6) three-arg ``workflow foo bar`` — usage to stderr + exit 2.
  (7) happy-path rollback seeded with v1 stable + v2 stable (prev=v1) —
       JSON stdout match (per-key, float tolerance 1e-6, int tolerance
       1 s for ``promoted_at``), exit 0, DB row diff confirming v2 is
       retired and v1 is the now-current stable with promoted_at ~ now.
  (8) rollback with no stable row — both exit 1 with stderr matching
       ``version_rollback: no stable version found for {kind}/{name}``.
  (9) rollback when stable has no ``previous_stable_version`` — both
       exit 1 with stderr matching
       ``version_rollback: no previous stable version recorded for {vid}``.

Notes:

  * The bash script sources ``lib/version_registry.sh`` at runtime,
    which re-creates the ``version_registry`` table if missing — so
    once bash is invoked once (in any of the bash calls below), the
    bash-side DB gets the table. For the py-side DB and for any
    *seeding* SQL we run before bash has touched the DB, we call
    ``mini_ork.registries.version_registry.ensure_table(db)`` to mirror
    the bash-side schema-bootstrap path explicitly.
  * ``db/init.sh`` does NOT create the ``version_registry`` table —
    migration ``0011_evolution.sql`` only creates
    ``version_registry_pointers`` (a separate, lighter-weight table).
    The ``version_registry`` table itself is created lazily by
    ``_ver_ensure_table`` in ``lib/version_registry.sh`` on first
    call.
  * ``promoted_at`` is set by ``int(time.time())`` inside the rollback
    SQL on both sides. To make the happy-path test deterministic, we
    parse both outputs as JSON and compare key-by-key with a 1-second
    tolerance on ``promoted_at`` (int drift, not float).
  * Two parallel DBs (``bash_db``, ``py_db``) are used for the
    happy-path test so each side's mutation (v2.retired, v1.stable
    with new promoted_at) doesn't pollute the other.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import rollback as py
from mini_ork.registries import version_registry as vr

SH = REPO / "bin" / "mini-ork-rollback"
INIT_SH = REPO / "db" / "init.sh"

# Subprocess env: pin TZ so any datetime formatting (none used here,
# but consistent with sibling parity tests) is identical between
# bash and py runs.
_SUBPROC_ENV_BASE = {**os.environ, "TZ": "UTC"}

# Maximum tolerated drift on the int epoch-second ``promoted_at``
# field between bash and Python invocations of the rollback.
_PROMOTED_AT_TOLERANCE_S = 2


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
def _which_tools() -> None:
    for tool in ("bash", "sqlite3", "python3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} not on PATH")
    if not SH.exists():
        pytest.skip(f"missing bin/mini-ork-rollback at {SH}")
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """One fresh mini-ork SQLite DB per test (read-only case) plus a
    paired copy for happy-path parity (bash DB vs py DB).

    The bash + py calls each mutate the DB (UPDATE v2.retired, UPDATE
    v1.stable promoted_at=now), so a single shared DB cannot host both
    calls in the happy-path case — bash would retire v2 then py would
    see no current-stable-with-prev, fail with the wrong error, and
    the parity gate would diverge.
    """
    _which_tools()
    home = tmp_path / "home"
    home.mkdir()
    db = str(home / "state.db")
    bash_db = str(home / "bash.db")
    py_db = str(home / "py.db")

    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**_SUBPROC_ENV_BASE, "MINI_ORK_HOME": str(home),
             "MINI_ORK_DB": db},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstderr={r.stderr}")

    # Fresh copies for the happy-path test (each gets its own mini-ork
    # schema seeded by copying the just-initialised DB; WAL files aren't
    # needed because we only do point reads + UPDATE).
    shutil.copyfile(db, bash_db)
    shutil.copyfile(db, py_db)

    # version_registry table is NOT created by db/init.sh — bash's
    # _ver_ensure_table creates it lazily. We ensure it exists in both
    # DBs explicitly so seeding SQL (which runs before any bash
    # invocation) can INSERT into it.
    vr.ensure_table(bash_db)
    vr.ensure_table(py_db)

    # Default DB is the one the Python port reads (env-controlled);
    # tests override per-call as needed via env on the helpers.
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("TZ", "UTC")
    return {"home": str(home), "db": db,
            "bash_db": bash_db, "py_db": py_db,
            "tmp_path": tmp_path}


def _bash_run(args: list[str], *, db: str) -> subprocess.CompletedProcess:
    """Invoke the live ``bin/mini-ork-rollback`` bash with the given args.

    The bash script exports MINI_ORK_DB / MINI_ORK_HOME itself (with
    defaults), but we set them explicitly so the test is hermetic.
    """
    return subprocess.run(
        ["bash", str(SH), *args],
        env={**_SUBPROC_ENV_BASE, "MINI_ORK_DB": db,
             "MINI_ORK_HOME": str(Path(db).parent)},
        capture_output=True, text=True,
    )


def _py_main(args: list[str], *, db: str) -> tuple[int, str, str]:
    """Invoke the Python port's main(argv) and capture stdout / stderr.

    We swap sys.stdout / sys.stderr around the call so the parity test
    can read the same byte streams the bash subprocess produced.
    """
    import io
    old_env = os.environ.get("MINI_ORK_DB")
    os.environ["MINI_ORK_DB"] = db
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = py.main(list(args))
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        if old_env is None:
            os.environ.pop("MINI_ORK_DB", None)
        else:
            os.environ["MINI_ORK_DB"] = old_env
    return rc, out.getvalue(), err.getvalue()


def _sql(db: str, stmt: str, params: tuple = ()) -> None:
    """Run a single parameterised DML/DDL statement against the DB."""
    con = sqlite3.connect(db)
    try:
        con.execute(stmt, params)
        con.commit()
    finally:
        con.close()


def _sql_query(db: str, sql: str, params: tuple = ()) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()
    return rows


def _seed_two_stables(db: str, kind: str, name: str,
                      v1_id: str, v2_id: str,
                      t1: int = 100, t2: int = 200) -> None:
    """Seed ``v1`` as the original stable (no previous) and ``v2`` as
    the current stable with ``previous_stable_version = v1``.

    Mirrors the approach in ``test_version_registry_py.py``:
    ``version_register`` does not set ``promoted_at``, so we insert
    directly via SQL with explicit values for both rows. This avoids
    any dependency on the bash / py register-order parity — we are
    shaping the DB into the exact state ``version_rollback`` expects.

    Note that ``version_registry.register`` does not error on a
    promoted-at-NULL row; it only sets ``created_at`` (which is the
    only NOT NULL column besides version_id/kind/name/payload). Using
    raw SQL here keeps the test independent of register's evolving
    semantics.
    """
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO version_registry "
            "(version_id, kind, name, status, payload, "
            " previous_stable_version, utility_score, promoted_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (v1_id, kind, name, "stable", json.dumps({"name": name}),
             None, 0.5, t1, t1),
        )
        con.execute(
            "INSERT INTO version_registry "
            "(version_id, kind, name, status, payload, "
            " previous_stable_version, utility_score, promoted_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (v2_id, kind, name, "stable", json.dumps({"name": name}),
             v1_id, 0.5, t2, t2),
        )
        con.commit()
    finally:
        con.close()


def _now() -> int:
    return int(time.time())


def _assert_json_parity(bash_stdout: str, py_stdout: str) -> None:
    """Compare two JSON stdout strings key-by-key with a small drift
    tolerance on ``promoted_at`` (int seconds) and 1e-6 on any float
    fields. Raises ``AssertionError`` with a diff marker on mismatch.
    """
    a = json.loads(bash_stdout)
    b = json.loads(py_stdout)
    assert set(a.keys()) == set(b.keys()), (
        f"key sets differ:\n  bash={sorted(a.keys())}\n  py  ={sorted(b.keys())}"
    )
    for k in a.keys():
        va, vb = a[k], b[k]
        if va is None or vb is None:
            assert va == vb, f"{k}: {va!r} vs {vb!r}"
            continue
        if k == "promoted_at":
            assert isinstance(va, int) and isinstance(vb, int), (
                f"promoted_at must be int: {va!r} vs {vb!r}"
            )
            assert abs(va - vb) <= _PROMOTED_AT_TOLERANCE_S, (
                f"promoted_at drift: bash={va} py={vb} "
                f"(tolerance={_PROMOTED_AT_TOLERANCE_S}s)"
            )
            continue
        if isinstance(va, float) and isinstance(vb, float):
            assert abs(va - vb) <= 1e-6, f"{k}: {va!r} vs {vb!r}"
            continue
        assert va == vb, f"{k}: bash={va!r} py={vb!r}"


def _assert_help_parity(bash_stdout: str) -> None:
    """Compare bash help stdout against the Python module's HELP_TEXT."""
    assert bash_stdout == py.help_text(), (
        f"help text mismatch:\n"
        f"  bash first 200: {bash_stdout[:200]!r}\n"
        f"  py   first 200: {py.help_text()[:200]!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (1) --help — usage to stdout, exit 0
# ─────────────────────────────────────────────────────────────────────────────
def test_help_long_flag(temp_db):
    """``--help`` → bash prints heredoc body to stdout + exit 0.

    Python's ``py.help_text()`` must be byte-equal to that body so the
    CLI dispatch path (``py.main(['--help'])``) emits the same string.
    """
    bash_r = _bash_run(["--help"], db=temp_db["db"])
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    assert bash_r.stderr == "", f"bash --help leaked to stderr: {bash_r.stderr!r}"
    _assert_help_parity(bash_r.stdout)

    # And via the dispatcher path (with sys.stdout captured):
    rc, out, err = _py_main(["--help"], db=temp_db["db"])
    assert rc == 0
    assert err == ""
    assert out == py.help_text()


# ─────────────────────────────────────────────────────────────────────────────
# (2) -h alias — same as --help
# ─────────────────────────────────────────────────────────────────────────────
def test_help_short_flag(temp_db):
    """``-h`` alias → exact same bytes as ``--help`` + exit 0."""
    bash_r = _bash_run(["-h"], db=temp_db["db"])
    assert bash_r.returncode == 0, f"bash failed: {bash_r.stderr}"
    assert bash_r.stderr == ""
    _assert_help_parity(bash_r.stdout)

    rc, out, err = _py_main(["-h"], db=temp_db["db"])
    assert rc == 0
    assert err == ""
    assert out == py.help_text()


# ─────────────────────────────────────────────────────────────────────────────
# (3) no args — usage to stderr, exit 2
# ─────────────────────────────────────────────────────────────────────────────
def test_no_args(temp_db):
    """Zero positional args → bash's kind="" fails the kind check →
    usage to stderr + exit 2. Same for Python.
    """
    bash_r = _bash_run([], db=temp_db["db"])
    assert bash_r.returncode == 2, f"bash rc={bash_r.returncode}"
    assert bash_r.stdout == "", f"bash leaked usage to stdout: {bash_r.stdout!r}"
    assert bash_r.stderr == py.help_text(), (
        f"bash stderr mismatch:\n"
        f"  bash: {bash_r.stderr!r}\n"
        f"  py  : {py.help_text()!r}"
    )

    rc, out, err = _py_main([], db=temp_db["db"])
    assert rc == 2
    assert out == ""
    assert err == py.help_text()


# ─────────────────────────────────────────────────────────────────────────────
# (4) invalid kind — usage to stderr, exit 2
# ─────────────────────────────────────────────────────────────────────────────
def test_invalid_kind(temp_db):
    """kind="unknown" fails ``kind != workflow && kind != agent`` →
    usage to stderr + exit 2.
    """
    bash_r = _bash_run(["unknown", "x"], db=temp_db["db"])
    assert bash_r.returncode == 2, f"bash rc={bash_r.returncode}"
    assert bash_r.stdout == ""
    assert bash_r.stderr == py.help_text()

    rc, out, err = _py_main(["unknown", "x"], db=temp_db["db"])
    assert rc == 2
    assert out == ""
    assert err == py.help_text()


# ─────────────────────────────────────────────────────────────────────────────
# (5) missing name — usage to stderr, exit 2
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_name(temp_db):
    """Only kind is supplied → ``-z "$name"`` → usage to stderr + exit 2."""
    bash_r = _bash_run(["workflow"], db=temp_db["db"])
    assert bash_r.returncode == 2, f"bash rc={bash_r.returncode}"
    assert bash_r.stdout == ""
    assert bash_r.stderr == py.help_text()

    rc, out, err = _py_main(["workflow"], db=temp_db["db"])
    assert rc == 2
    assert out == ""
    assert err == py.help_text()


# ─────────────────────────────────────────────────────────────────────────────
# (6) three-arg form — usage to stderr, exit 2
# ─────────────────────────────────────────────────────────────────────────────
def test_three_args(temp_db):
    """``workflow foo bar`` → kind passes check, but ``$# -ne 2`` →
    usage to stderr + exit 2.
    """
    bash_r = _bash_run(["workflow", "foo", "bar"], db=temp_db["db"])
    assert bash_r.returncode == 2, f"bash rc={bash_r.returncode}"
    assert bash_r.stdout == ""
    assert bash_r.stderr == py.help_text()

    rc, out, err = _py_main(["workflow", "foo", "bar"], db=temp_db["db"])
    assert rc == 2
    assert out == ""
    assert err == py.help_text()


# ─────────────────────────────────────────────────────────────────────────────
# (7) happy path — seeded with two stables, JSON stdout + DB diff
# ─────────────────────────────────────────────────────────────────────────────
def test_happy_path_rollback(temp_db):
    """Seed ``v1`` (stable, no prev) + ``v2`` (stable, prev=v1). Run bash
    on ``bash_db`` then Python on ``py_db``. Stdout JSONs must match
    per-key (int drift ≤ 2 s on promoted_at, float 1e-6 elsewhere).
    Both must exit 0.

    After the rollback:
      * v2.status == 'retired'
      * v1.status == 'stable' (still, after promotion)
      * v1.promoted_at ~= now (int seconds, updated by the rollback SQL)
      * v1.previous_stable_version unchanged (still NULL — we never
        set it because there was no stable before v1)
    """
    kind, name = "workflow", "svc"
    v1, v2 = "v-wor-rb001", "v-wor-rb002"
    _seed_two_stables(temp_db["bash_db"], kind, name, v1, v2)
    _seed_two_stables(temp_db["py_db"], kind, name, v1, v2)

    before = _now()
    bash_r = _bash_run([kind, name], db=temp_db["bash_db"])
    rc_py, out_py, err_py = _py_main([kind, name], db=temp_db["py_db"])
    after = _now()

    # ── stdout / exit-code parity ──────────────────────────────────────────
    assert bash_r.returncode == 0, (
        f"bash happy-path failed: rc={bash_r.returncode} stderr={bash_r.stderr!r}"
    )
    assert rc_py == 0, (
        f"py happy-path failed: rc={rc_py} stderr={err_py!r}"
    )
    assert bash_r.stderr == "", f"bash stderr leaked: {bash_r.stderr!r}"
    assert err_py == "", f"py stderr leaked: {err_py!r}"
    _assert_json_parity(bash_r.stdout, out_py)

    # ── DB diff (bash side) ────────────────────────────────────────────────
    # bash retired v2 and promoted v1 — verify the resulting state.
    bash_rows = _sql_query(
        temp_db["bash_db"],
        "SELECT version_id, status, promoted_at FROM version_registry "
        "WHERE version_id IN (?, ?) ORDER BY version_id",
        (v1, v2),
    )
    by_id = {r["version_id"]: r for r in bash_rows}
    assert by_id[v1]["status"] == "stable", by_id
    assert by_id[v2]["status"] == "retired", by_id
    # promoted_at is now within [before-1, after+1] (allow 1 s slack on
    # each side; the SQL is int(time.time()) and the bash heredoc /
    # Python port can straddle a second boundary).
    pa = by_id[v1]["promoted_at"]
    assert before - 1 <= pa <= after + 1, (
        f"v1.promoted_at={pa} not within [{before-1},{after+1}]"
    )

    # ── DB diff (py side) — same shape ────────────────────────────────────
    py_rows = _sql_query(
        temp_db["py_db"],
        "SELECT version_id, status, promoted_at FROM version_registry "
        "WHERE version_id IN (?, ?) ORDER BY version_id",
        (v1, v2),
    )
    by_id_p = {r["version_id"]: r for r in py_rows}
    assert by_id_p[v1]["status"] == "stable"
    assert by_id_p[v2]["status"] == "retired"
    # bash and py produced very close timestamps (within tolerance).
    assert abs(by_id[v1]["promoted_at"] - by_id_p[v1]["promoted_at"]) <= 2, (
        f"bash.promoted_at={by_id[v1]['promoted_at']} "
        f"py.promoted_at={by_id_p[v1]['promoted_at']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (8) no stable row — both exit 1 with stderr matching the rollback error
# ─────────────────────────────────────────────────────────────────────────────
def test_rollback_no_stable(temp_db):
    """Empty DB → ``version_rollback`` can't find a current stable →
    emits ``version_rollback: no stable version found for {kind}/{name}``
    to stderr and exits 1 (via bash's set -e propagating the python
    heredoc's sys.exit(1)).
    """
    expected_err = "version_rollback: no stable version found for workflow/svc"
    bash_r = _bash_run(["workflow", "svc"], db=temp_db["bash_db"])
    assert bash_r.returncode == 1, (
        f"bash rc={bash_r.returncode}; stderr={bash_r.stderr!r}"
    )
    assert bash_r.stdout == "", f"bash leaked stdout: {bash_r.stdout!r}"
    assert bash_r.stderr.strip() == expected_err, (
        f"bash stderr: {bash_r.stderr!r}"
    )

    rc, out, err = _py_main(["workflow", "svc"], db=temp_db["py_db"])
    assert rc == 1
    assert out == ""
    assert err.strip() == expected_err, f"py stderr: {err!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (9) stable has no previous_stable_version — both exit 1
# ─────────────────────────────────────────────────────────────────────────────
def test_rollback_no_previous_stable(temp_db):
    """A stable row exists but its ``previous_stable_version`` is NULL
    (e.g. v1 was registered as the first stable, no prior stable
    exists). ``version_rollback`` exits 1 with
    ``version_rollback: no previous stable version recorded for <vid>``.
    """
    kind, name = "workflow", "svc"
    v1 = "v-wor-np001"
    # Seed ONLY v1 as stable with previous_stable_version=NULL.
    _sql(temp_db["bash_db"],
         "INSERT INTO version_registry "
         "(version_id, kind, name, status, payload, "
         " previous_stable_version, utility_score, promoted_at, created_at) "
         "VALUES (?,?,?,?,?,?,?,?,?)",
         (v1, kind, name, "stable", json.dumps({"name": name}),
          None, 0.5, 100, 100))
    _sql(temp_db["py_db"],
         "INSERT INTO version_registry "
         "(version_id, kind, name, status, payload, "
         " previous_stable_version, utility_score, promoted_at, created_at) "
         "VALUES (?,?,?,?,?,?,?,?,?)",
         (v1, kind, name, "stable", json.dumps({"name": name}),
          None, 0.5, 100, 100))

    expected_err = (
        f"version_rollback: no previous stable version recorded for {v1}"
    )
    bash_r = _bash_run([kind, name], db=temp_db["bash_db"])
    assert bash_r.returncode == 1, (
        f"bash rc={bash_r.returncode}; stderr={bash_r.stderr!r}"
    )
    assert bash_r.stdout == ""
    assert bash_r.stderr.strip() == expected_err

    rc, out, err = _py_main([kind, name], db=temp_db["py_db"])
    assert rc == 1
    assert out == ""
    assert err.strip() == expected_err