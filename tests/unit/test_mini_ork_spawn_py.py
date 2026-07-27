"""Unit tests: mini_ork.cli.spawn (bash parity halves removed; formerly vs bin/mini-ork-spawn).

Each test invokes the Python port (``python -m mini_ork.cli.spawn``)
against a temp DB seeded by ``db/init.sh`` and asserts:

  * stdout ``key=value`` lines (``spawn_id`` stem is ``sp-``; volatile hex
    suffix not pinned).
  * exit codes.
  * stderr error phrases on validation failures.
  * ``run_spawns`` / ``run_events`` / ``task_runs`` rows (authority_level
    floats 1e-6).

Cases:

  (a) --help stdout + exit 0
  (b) missing --parent-run: exit 2, stderr phrase
  (c) missing --kickoff: exit 2
  (d) kickoff not found: exit 2, stderr phrase
  (e) state.db not found: exit 2, stderr phrase
  (f) --no-execute happy path: stdout lines + run_spawns/run_events/task_runs
  (g) depth inference from seeded parent depth=1 → child depth=2
  (h) MINI_ORK_CHILD_AUTHORITY env: authority_level stored at 1e-6
  (i) --allow-child-spawn: run_spawns.allow_child_spawn=1
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

INIT_SH = REPO / "db" / "init.sh"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def home(tmp_path):
    """Temp MINI_ORK_HOME under tmp_path."""
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def py_db(home):
    """Seed a temp state.db for the Python port."""
    dbp = str(home / "py.db")
    r = subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"db/init.sh failed: rc={r.returncode}\nstderr={r.stderr}")
    return dbp


def _run_py(args: list[str], *, db: str, home,
            extra_env: dict | None = None) -> tuple[int, str, str]:
    """Invoke `python -m mini_ork.cli.spawn` against the temp DB."""
    env = {
        **os.environ,
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_HOME": str(home),
        "MINI_ORK_DB": db,
        "MINI_ORK_CHILD_AUTHORITY": "0.5",
    }
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.spawn", *args],
        env=env, capture_output=True, text=True, cwd=str(REPO),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _row_dicts(db: str, table: str) -> list[dict]:
    con = sqlite3.connect(db)
    try:
        cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
        rows = con.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()


def _stem(value: str | None, *, sep: str = "-") -> str:
    """Mask volatile hex12 suffix: keep `prefix-` only."""
    if value is None:
        return ""
    return value.split(sep, 1)[0] + sep


def _parse_keyed_lines(text: str) -> dict[str, str]:
    """Parse `key=value` stdout into a dict."""
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _seed_parent(db: str, parent_id: str, *, depth: int | None = None) -> None:
    """Seed a task_runs row + an optional run_spawns row for depth inference tests."""
    con = sqlite3.connect(db)
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO task_runs(id, task_class, recipe, kickoff_path, status, created_at, updated_at)
            VALUES (?, 'code_fix', NULL, ?, 'classified', 0, 0)
            """,
            (parent_id, "/tmp/k.md"),
        )
        if depth is not None:
            con.execute(
                """
                INSERT OR REPLACE INTO run_spawns(
                  spawn_id, parent_run_id, child_run_id, root_run_id, depth, recipe,
                  kickoff_path, child_workspace, authority_level, allow_child_spawn,
                  status, policy_snapshot_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 'approved', '{}', 0, 0)
                """,
                (
                    f"sp-seed-{parent_id}", f"grandparent-{parent_id}", parent_id,
                    f"grandparent-{parent_id}", depth,
                    "/tmp/seed-k.md", "/tmp/seed-ws", 0.3, 0,
                ),
            )
        con.commit()
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────────────────
# (a) --help
# ─────────────────────────────────────────────────────────────────────────────
def test_help_prints_usage_and_exits_0(home, py_db):
    """The port prints the usage block and exits 0 on --help."""
    py_rc, py_out, py_err = _run_py(["--help"], db=py_db, home=home)
    assert py_rc == 0, f"py --help failed: {py_err}"
    assert "Usage: mini-ork spawn" in py_out
    assert "--parent-run" in py_out
    assert "--kickoff" in py_out


# ─────────────────────────────────────────────────────────────────────────────
# (b) missing --parent-run
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_parent_run_exits_2(home, py_db, tmp_path):
    """Exit 2 with stderr containing 'is required' when --parent-run is omitted."""
    kickoff = tmp_path / "k.md"; kickoff.write_text("# k\n")

    py_rc, _, py_err = _run_py(["--kickoff", str(kickoff)], db=py_db, home=home)
    assert py_rc == 2, f"py rc={py_rc}, stderr={py_err!r}"
    assert "--parent-run" in py_err and "is required" in py_err, (
        f"py stderr missing phrase: {py_err!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# (c) missing --kickoff
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_kickoff_exits_2(home, py_db):
    """Exit 2 with stderr mentioning --kickoff when --kickoff is omitted."""
    py_rc, _, py_err = _run_py(["--parent-run", "p-x"], db=py_db, home=home)
    assert py_rc == 2, py_err
    assert "--kickoff" in py_err and "is required" in py_err


# ─────────────────────────────────────────────────────────────────────────────
# (d) kickoff file not found
# ─────────────────────────────────────────────────────────────────────────────
def test_kickoff_not_found_exits_2(home, py_db, tmp_path):
    """Exit 2 with stderr 'kickoff not found: …' when --kickoff points at a
    missing file."""
    missing = tmp_path / "does-not-exist.md"

    py_rc, _, py_err = _run_py(["--parent-run", "p-d", "--kickoff", str(missing)],
                               db=py_db, home=home)
    assert py_rc == 2, py_err
    assert "kickoff not found" in py_err
    assert str(missing) in py_err


# ─────────────────────────────────────────────────────────────────────────────
# (e) state.db not found
# ─────────────────────────────────────────────────────────────────────────────
def test_state_db_not_found_exits_2(home, tmp_path):
    """Exit 2 when state.db does not exist (no init.sh ran)."""
    kickoff = tmp_path / "k.md"; kickoff.write_text("# k\n")
    missing_db = str(home / "ghost.db")
    env = {"MINI_ORK_DB": missing_db, "MINI_ORK_HOME": str(home)}

    py_rc, _, py_err = _run_py(["--parent-run", "p-e", "--kickoff", str(kickoff)],
                               db=missing_db, home=home, extra_env=env)
    assert py_rc == 2, py_err
    assert "state.db not found" in py_err, py_err


# ─────────────────────────────────────────────────────────────────────────────
# (f) --no-execute happy path: stdout lines + run_spawns/run_events/task_runs
# ─────────────────────────────────────────────────────────────────────────────
def test_no_execute_happy_path(home, py_db, tmp_path):
    """--no-execute writes run_spawns (status=approved) + run_events (spawn.approved)
    + task_runs (UPSERT)."""
    kickoff = tmp_path / "k.md"; kickoff.write_text("# k\n")
    parent = "parent-f"

    _seed_parent(py_db, parent)

    args = [
        "--parent-run", parent,
        "--kickoff", str(kickoff),
        "--child-run", "child-f",
        "--depth", "1",
        "--authority", "0.5",
        "--recipe", "code-fix",
        "--no-execute",
    ]
    py_rc, py_out, py_err = _run_py(args, db=py_db, home=home)
    assert py_rc == 0, f"py failed: {py_err}"

    py_kv = _parse_keyed_lines(py_out)
    assert _stem(py_kv["spawn_id"]) == "sp-"
    assert py_kv["parent_run_id"] == parent
    assert py_kv["child_run_id"] == "child-f"
    assert py_kv["depth"] == "1"
    assert py_kv["allow_child_spawn"] == "0"
    assert py_kv["spawn_status"] == "approved"
    assert py_kv["child_workspace"] and py_kv["child_kickoff"]

    # DB state: run_spawns, run_events, task_runs.
    sp = _row_dicts(py_db, "run_spawns")
    assert len(sp) == 1, f"run_spawns count: {sp}"
    assert sp[0]["child_run_id"] == "child-f"
    assert sp[0]["status"] == "approved"
    assert abs(float(sp[0]["authority_level"]) - 0.5) <= 1e-6, (
        f"authority_level={sp[0]['authority_level']!r}"
    )

    ev = _row_dicts(py_db, "run_events")
    assert len(ev) == 1, f"run_events count: {ev}"
    assert ev[0]["event_type"] == "spawn.approved"
    assert _stem(ev[0]["event_id"]) == "ev-"
    # event_id must contain the literal child_run_id.
    assert "child-f" in ev[0]["event_id"], (
        f"event_id missing child_run_id: {ev[0]['event_id']!r}"
    )

    tr = _row_dicts(py_db, "task_runs")
    child_tr = next(r for r in tr if r["id"] == "child-f")
    assert child_tr["task_class"] == "code_fix"  # recipe dashes → underscores
    assert child_tr["status"] == "classified"


# ─────────────────────────────────────────────────────────────────────────────
# (g) depth inference from seeded parent depth=1 → child depth=2
# ─────────────────────────────────────────────────────────────────────────────
def test_depth_inference_from_parent(home, py_db, tmp_path):
    """When --depth is omitted, the port looks up the parent's depth in
    run_spawns and uses parent.depth + 1. Seed parent with depth=1; expect
    child depth=2."""
    kickoff = tmp_path / "k.md"; kickoff.write_text("# k\n")
    parent = "parent-g"

    _seed_parent(py_db, parent, depth=1)

    args = [
        "--parent-run", parent,
        "--kickoff", str(kickoff),
        "--child-run", "child-g",
        "--no-execute",
    ]
    py_rc, py_out, py_err = _run_py(args, db=py_db, home=home)
    assert py_rc == 0, f"py failed: {py_err}"

    py_kv = _parse_keyed_lines(py_out)
    assert py_kv["depth"] == "2", py_kv

    # DB row depth also matches (the seed row with the parent's depth is
    # still present in the table).
    sp = _row_dicts(py_db, "run_spawns")
    child_rows = [r for r in sp if r["child_run_id"] == "child-g"]
    assert len(child_rows) == 1, f"expected 1 child-g row, got {child_rows}"
    assert child_rows[0]["depth"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# (h) MINI_ORK_CHILD_AUTHORITY env var floats (1e-6)
# ─────────────────────────────────────────────────────────────────────────────
def test_authority_from_env_var(home, py_db, tmp_path):
    """The port reads MINI_ORK_CHILD_AUTHORITY when --authority is not passed."""
    kickoff = tmp_path / "k.md"; kickoff.write_text("# k\n")
    parent = "parent-h"

    _seed_parent(py_db, parent)

    args = [
        "--parent-run", parent,
        "--kickoff", str(kickoff),
        "--child-run", "child-h",
        "--no-execute",
    ]
    extra = {"MINI_ORK_CHILD_AUTHORITY": "0.725"}
    py_rc, _, py_err = _run_py(args, db=py_db, home=home, extra_env=extra)
    assert py_rc == 0, f"py failed: {py_err}"

    py_sp = _row_dicts(py_db, "run_spawns")[0]
    assert abs(float(py_sp["authority_level"]) - 0.725) <= 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# (i) --allow-child-spawn: run_spawns.allow_child_spawn=1
# ─────────────────────────────────────────────────────────────────────────────
def test_allow_child_spawn_flag(home, py_db, tmp_path):
    """The port stores allow_child_spawn=1 in run_spawns when
    --allow-child-spawn is set."""
    kickoff = tmp_path / "k.md"; kickoff.write_text("# k\n")
    parent = "parent-i"

    _seed_parent(py_db, parent)

    args = [
        "--parent-run", parent,
        "--kickoff", str(kickoff),
        "--child-run", "child-i",
        "--no-execute",
        "--allow-child-spawn",
    ]
    py_rc, py_out, py_err = _run_py(args, db=py_db, home=home)
    assert py_rc == 0, f"py failed: {py_err}"

    py_kv = _parse_keyed_lines(py_out)
    assert py_kv["allow_child_spawn"] == "1"

    sp = _row_dicts(py_db, "run_spawns")
    assert sp[0]["allow_child_spawn"] == 1
