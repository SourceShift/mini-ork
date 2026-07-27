"""Unit tests: ``mini_ork.cli.rollback`` (bash parity halves removed; formerly vs ``bin/mini-ork-rollback``).

Each test invokes the Python port's main(argv) with captured stdout /
stderr against a temp DB seeded by ``db/init.sh``, asserting exit codes,
stdout/stderr content, and DB state. No mocks.

Schema bootstrap: ``db/init.sh`` applies migration ``0011_evolution.sql``
which creates ``version_registry_pointers`` (a separate, lighter-weight
table) — NOT ``version_registry`` itself. The ``version_registry`` table
is created lazily by ``mini_ork.registries.version_registry.ensure_table``
(the port of bash's ``_ver_ensure_table``), which the fixture calls
explicitly so seeding SQL can INSERT into it.

Cases (9):

  (1) ``--help`` — help text on stdout + exit 0.
  (2) ``-h`` alias — same as --help.
  (3) no args — usage to stderr + exit 2.
  (4) invalid kind ``unknown`` — usage to stderr + exit 2.
  (5) missing name ``workflow`` only — usage to stderr + exit 2.
  (6) three-arg ``workflow foo bar`` — usage to stderr + exit 2.
  (7) happy-path rollback seeded with v1 stable + v2 stable (prev=v1) —
       JSON stdout, exit 0, DB confirming v2 is retired and v1 is the
       now-current stable with promoted_at ~ now.
  (8) rollback with no stable row — exit 1 with stderr matching
       ``version_rollback: no stable version found for {kind}/{name}``.
  (9) rollback when stable has no ``previous_stable_version`` — exit 1
       with stderr matching
       ``version_rollback: no previous stable version recorded for {vid}``.
"""
from __future__ import annotations

import json
import os
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

INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """One fresh mini-ork SQLite DB per test with version_registry ensured."""
    if not INIT_SH.exists():
        pytest.skip(f"missing db/init.sh at {INIT_SH}")
    home = tmp_path / "home"
    home.mkdir()
    db = str(home / "state.db")

    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "TZ": "UTC", "MINI_ORK_HOME": str(home),
             "MINI_ORK_DB": db},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstderr={r.stderr}")

    # version_registry table is NOT created by db/init.sh — ensure it
    # exists explicitly so seeding SQL can INSERT into it.
    vr.ensure_table(db)

    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("TZ", "UTC")
    return {"home": str(home), "db": db, "tmp_path": tmp_path}


def _py_main(args: list[str], *, db: str) -> tuple[int, str, str]:
    """Invoke the Python port's main(argv) and capture stdout / stderr."""
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
    the current stable with ``previous_stable_version = v1``."""
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


# ─────────────────────────────────────────────────────────────────────────────
# (1) --help — usage to stdout, exit 0
# ─────────────────────────────────────────────────────────────────────────────
def test_help_long_flag(temp_db):
    rc, out, err = _py_main(["--help"], db=temp_db["db"])
    assert rc == 0
    assert err == ""
    assert out == py.help_text()


# ─────────────────────────────────────────────────────────────────────────────
# (2) -h alias — same as --help
# ─────────────────────────────────────────────────────────────────────────────
def test_help_short_flag(temp_db):
    rc, out, err = _py_main(["-h"], db=temp_db["db"])
    assert rc == 0
    assert err == ""
    assert out == py.help_text()


# ─────────────────────────────────────────────────────────────────────────────
# (3) no args — usage to stderr, exit 2
# ─────────────────────────────────────────────────────────────────────────────
def test_no_args(temp_db):
    rc, out, err = _py_main([], db=temp_db["db"])
    assert rc == 2
    assert out == ""
    assert err == py.help_text()


# ─────────────────────────────────────────────────────────────────────────────
# (4) invalid kind — usage to stderr, exit 2
# ─────────────────────────────────────────────────────────────────────────────
def test_invalid_kind(temp_db):
    rc, out, err = _py_main(["unknown", "x"], db=temp_db["db"])
    assert rc == 2
    assert out == ""
    assert err == py.help_text()


# ─────────────────────────────────────────────────────────────────────────────
# (5) missing name — usage to stderr, exit 2
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_name(temp_db):
    rc, out, err = _py_main(["workflow"], db=temp_db["db"])
    assert rc == 2
    assert out == ""
    assert err == py.help_text()


# ─────────────────────────────────────────────────────────────────────────────
# (6) three-arg form — usage to stderr, exit 2
# ─────────────────────────────────────────────────────────────────────────────
def test_three_args(temp_db):
    rc, out, err = _py_main(["workflow", "foo", "bar"], db=temp_db["db"])
    assert rc == 2
    assert out == ""
    assert err == py.help_text()


# ─────────────────────────────────────────────────────────────────────────────
# (7) happy path — seeded with two stables, JSON stdout + DB state
# ─────────────────────────────────────────────────────────────────────────────
def test_happy_path_rollback(temp_db):
    """Seed ``v1`` (stable, no prev) + ``v2`` (stable, prev=v1). After the
    rollback:
      * v2.status == 'retired'
      * v1.status == 'stable' (still, after promotion)
      * v1.promoted_at ~= now (int seconds, updated by the rollback SQL)
      * stdout is the promoted version's JSON row
    """
    kind, name = "workflow", "svc"
    v1, v2 = "v-wor-rb001", "v-wor-rb002"
    _seed_two_stables(temp_db["db"], kind, name, v1, v2)

    before = _now()
    rc_py, out_py, err_py = _py_main([kind, name], db=temp_db["db"])
    after = _now()

    assert rc_py == 0, f"py happy-path failed: rc={rc_py} stderr={err_py!r}"
    assert err_py == "", f"py stderr leaked: {err_py!r}"

    # stdout JSON is the promoted (previous stable) version row
    doc = json.loads(out_py)
    assert doc["version_id"] == v1
    assert doc["status"] == "stable"

    # DB state: v2 retired, v1 stable with fresh promoted_at.
    py_rows = _sql_query(
        temp_db["db"],
        "SELECT version_id, status, promoted_at FROM version_registry "
        "WHERE version_id IN (?, ?) ORDER BY version_id",
        (v1, v2),
    )
    by_id = {r["version_id"]: r for r in py_rows}
    assert by_id[v1]["status"] == "stable"
    assert by_id[v2]["status"] == "retired"
    pa = by_id[v1]["promoted_at"]
    assert before - 1 <= pa <= after + 1, (
        f"v1.promoted_at={pa} not within [{before-1},{after+1}]"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (8) no stable row — exit 1 with the rollback error on stderr
# ─────────────────────────────────────────────────────────────────────────────
def test_rollback_no_stable(temp_db):
    expected_err = "version_rollback: no stable version found for workflow/svc"
    rc, out, err = _py_main(["workflow", "svc"], db=temp_db["db"])
    assert rc == 1
    assert out == ""
    assert err.strip() == expected_err, f"py stderr: {err!r}"


# ─────────────────────────────────────────────────────────────────────────────
# (9) stable has no previous_stable_version — exit 1
# ─────────────────────────────────────────────────────────────────────────────
def test_rollback_no_previous_stable(temp_db):
    kind, name = "workflow", "svc"
    v1 = "v-wor-np001"
    # Seed ONLY v1 as stable with previous_stable_version=NULL.
    _sql(temp_db["db"],
         "INSERT INTO version_registry "
         "(version_id, kind, name, status, payload, "
         " previous_stable_version, utility_score, promoted_at, created_at) "
         "VALUES (?,?,?,?,?,?,?,?,?)",
         (v1, kind, name, "stable", json.dumps({"name": name}),
          None, 0.5, 100, 100))

    expected_err = (
        f"version_rollback: no previous stable version recorded for {v1}"
    )
    rc, out, err = _py_main([kind, name], db=temp_db["db"])
    assert rc == 1
    assert out == ""
    assert err.strip() == expected_err
