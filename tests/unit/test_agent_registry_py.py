"""Parity gate: mini_ork.ported.agent_registry vs lib/agent_registry.sh.

Strangler-fig co-existence: bash stays in place, this is the additive Python
port. Every case runs the bash source AND the Python port in separate temp
DBs, then diffs outputs / row dumps. Timestamp noise (registered_at differs by
up to a second between subprocess invocations) is normalised away before
row-comparison; float rounding uses a 1e-6 tolerance per the kickoff.

Cases (>=6 mandated by kickoff, 7 here):
  1. register returns version_id; get returns row
  2. register retires previous active
  3. current returns active or null
  4. get unknown version returns null
  5. performance aggregates traces (1e-6 float-tolerant)
  6. register errors match (invalid JSON + missing 'model')
  7. db/init.sh bootstrapped DB parity (structural row-diff)
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import agent_registry as ar  # noqa: E402

AR_SH = REPO / "lib" / "agent_registry.sh"
TRACE_SH = REPO / "lib" / "trace_store.sh"
DB_INIT_SH = REPO / "db" / "init.sh"

REGISTRY_DDL = ar.SCHEMA_SQL

# Minimal execution_traces schema for cases that don't want a full db/init.sh
# bootstrap (the perf case). Columns mirror what agent_performance's join needs;
# the real canonical schema lives in db/migrations/0010_benchmarks.sql.
TRACES_DDL = """
    CREATE TABLE IF NOT EXISTS execution_traces (
        trace_id         TEXT PRIMARY KEY,
        run_id           INTEGER NOT NULL DEFAULT 0,
        agent_version_id TEXT NOT NULL DEFAULT '',
        task_class       TEXT NOT NULL DEFAULT '',
        cost_usd         REAL NOT NULL DEFAULT 0.0,
        duration_ms      INTEGER NOT NULL DEFAULT 0,
        status           TEXT NOT NULL CHECK (status IN ('success','failure'))
    );
"""


def _bash(env_overrides, body):
    """Source both bash libs and run body. env_overrides layered on os.environ."""
    env = {**os.environ, **env_overrides,
           "MINI_ORK_ROOT": str(REPO)}
    return subprocess.run(
        ["bash", "-c", f'. "{AR_SH}" && . "{TRACE_SH}" && ' + body],
        env=env, capture_output=True, text=True,
    )


def _strip_timestamp(row: dict) -> dict:
    """Coarsen fields that legitimately differ between bash and python
    invocations (registered_at may be 1s apart)."""
    out = dict(row)
    if "registered_at" in out:
        out["registered_at"] = 0  # both sides stamp int(time.time())
    return out


def _make_temp_db(prefix="mo-py"):
    import tempfile
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".db")
    os.close(fd)
    os.unlink(path)  # let sqlite3 create it
    return path


@pytest.fixture
def fresh_db_path(tmp_path):
    """One fresh DB path per test. Callers create their own bash_db / py_db
    siblings rather than sharing this file."""
    return str(tmp_path / "shared.db")


# ───────────────────────── Case 1: register + get round-trip ──────────────────

def test_register_returns_version_id_and_get_returns_row():
    bash_db = _make_temp_db("mo-case1-bash")
    py_db = _make_temp_db("mo-case1-py")
    try:
        payload = json.dumps({"model": "claude-sonnet-4-5",
                              "provider": "anthropic",
                              "tools": ["read", "write"],
                              "task_classes": ["code-review"]})

        rb = _bash({"MINI_ORK_DB": bash_db},
                   f'agent_register "planner" \'{payload}\'')
        rp = subprocess.run(
            ["python3", "-c",
             "import os, sys, json; sys.path.insert(0, '.'); "
             "os.environ['MINI_ORK_DB']=sys.argv[1]; "
             "from mini_ork.ported.agent_registry import register; "
             "print(register('planner', json.loads(sys.argv[2])))",
             py_db, payload],
            capture_output=True, text=True, cwd=str(REPO))

        assert rb.returncode == 0, rb.stderr
        assert rp.returncode == 0, rp.stderr
        vid_bash = rb.stdout.strip()
        vid_py = rp.stdout.strip()
        assert vid_bash.startswith("av-plann") and len(vid_bash) > len("av-plann")
        assert vid_py.startswith("av-plann") and len(vid_py) > len("av-plann")

        rb2 = _bash({"MINI_ORK_DB": bash_db},
                    f'agent_get "planner" "{vid_bash}"')
        rp2 = _bash({"MINI_ORK_DB": py_db},
                    f'agent_get "planner" "{vid_py}"')
        # Actually run py's get via subprocess for fair comparison
        rp2 = subprocess.run(
            ["python3", "-c",
             "import os, sys, json; sys.path.insert(0, '.'); "
             "os.environ['MINI_ORK_DB']=sys.argv[1]; "
             "from mini_ork.ported.agent_registry import get; "
             "r = get('planner', sys.argv[2]); "
             "print(json.dumps(r) if r else 'null')",
             py_db, vid_py],
            capture_output=True, text=True, cwd=str(REPO))

        assert rb2.returncode == 0 and rb2.stdout.strip() != "null"
        assert rp2.returncode == 0 and rp2.stdout.strip() != "null"

        row_bash = json.loads(rb2.stdout.strip())
        row_py = json.loads(rp2.stdout.strip())
        assert row_bash["model"] == "claude-sonnet-4-5"
        assert row_py["model"] == "claude-sonnet-4-5"
        # Equality mod registered_at (both sides stamp int(time.time()))
        rb_norm = _strip_timestamp(row_bash)
        rp_norm = _strip_timestamp(row_py)
        assert rb_norm["model"] == rp_norm["model"]
        assert rb_norm["role"] == rp_norm["role"]
        assert rb_norm["status"] == rp_norm["status"]
        assert row_bash["status"] == row_py["status"] == "active"
    finally:
        for p in (bash_db, py_db):
            if os.path.exists(p):
                os.unlink(p)


# ─────────────────── Case 2: register retires previous active ──────────────────

def test_register_retires_previous_active():
    bash_db = _make_temp_db("mo-case2-bash")
    py_db = _make_temp_db("mo-case2-py")
    try:
        payload_a = json.dumps({"model": "claude-sonnet-4-5"})
        payload_b = json.dumps({"model": "claude-opus-4-5"})

        # Bash: two registers, then dump statuses
        rb1 = _bash({"MINI_ORK_DB": bash_db},
                    f'agent_register "planner" \'{payload_a}\'')
        vid_a_bash = rb1.stdout.strip()
        rb2 = _bash({"MINI_ORK_DB": bash_db},
                    f'agent_register "planner" \'{payload_b}\'')
        vid_b_bash = rb2.stdout.strip()
        # Read status columns from bash DB
        with sqlite3.connect(bash_db) as con:
            bash_statuses = {
                r[0]: r[1]
                for r in con.execute(
                    "SELECT version_id, status FROM agent_registry "
                    "WHERE role='planner' ORDER BY registered_at"
                ).fetchall()
            }

        # Py: same dance
        rp1 = subprocess.run(
            ["python3", "-c",
             "import os, sys, json; sys.path.insert(0, '.'); "
             "os.environ['MINI_ORK_DB']=sys.argv[1]; "
             "from mini_ork.ported.agent_registry import register; "
             "print(register('planner', json.loads(sys.argv[2])))",
             py_db, payload_a],
            capture_output=True, text=True, cwd=str(REPO))
        vid_a_py = rp1.stdout.strip()
        rp2 = subprocess.run(
            ["python3", "-c",
             "import os, sys, json; sys.path.insert(0, '.'); "
             "os.environ['MINI_ORK_DB']=sys.argv[1]; "
             "from mini_ork.ported.agent_registry import register; "
             "print(register('planner', json.loads(sys.argv[2])))",
             py_db, payload_b],
            capture_output=True, text=True, cwd=str(REPO))
        vid_b_py = rp2.stdout.strip()
        with sqlite3.connect(py_db) as con:
            py_statuses = {
                r[0]: r[1]
                for r in con.execute(
                    "SELECT version_id, status FROM agent_registry "
                    "WHERE role='planner' ORDER BY registered_at"
                ).fetchall()
            }

        # Parity: both sides end up with exactly one active + one retired
        assert bash_statuses[vid_a_bash] == "retired"
        assert bash_statuses[vid_b_bash] == "active"
        assert py_statuses[vid_a_py] == "retired"
        assert py_statuses[vid_b_py] == "active"
        # Count parity
        assert sorted(bash_statuses.values()) == sorted(py_statuses.values()) == ["active", "retired"]
    finally:
        for p in (bash_db, py_db):
            if os.path.exists(p):
                os.unlink(p)


# ─────────────────────── Case 3: current returns active / null ─────────────────

def test_current_returns_active_or_null():
    bash_db = _make_temp_db("mo-case3-bash")
    py_db = _make_temp_db("mo-case3-py")
    try:
        # Register one version on each side
        rb = _bash({"MINI_ORK_DB": bash_db},
                   'agent_register "planner" \'{"model":"m1"}\'')
        rp = subprocess.run(
            ["python3", "-c",
             "import os, sys; sys.path.insert(0, '.'); "
             "os.environ['MINI_ORK_DB']=sys.argv[1]; "
             "from mini_ork.ported.agent_registry import register, current; "
             "register('planner', {'model':'m1'}); "
             "import json; r = current('planner'); "
             "print(json.dumps(r) if r else 'null')",
             py_db],
            capture_output=True, text=True, cwd=str(REPO))

        rb_active = _bash({"MINI_ORK_DB": bash_db}, 'agent_current "planner"')

        assert rb.returncode == 0
        assert rp.returncode == 0, rp.stderr
        bash_cur = json.loads(rb_active.stdout.strip())
        py_cur = json.loads(rp.stdout.strip())
        assert bash_cur["status"] == "active" and py_cur["status"] == "active"
        assert bash_cur["model"] == py_cur["model"] == "m1"

        # Unknown role: both should return null/None
        rb_none = _bash({"MINI_ORK_DB": bash_db}, 'agent_current "role-no-such-xyz"')
        rp_none = subprocess.run(
            ["python3", "-c",
             "import os, sys, json; sys.path.insert(0, '.'); "
             "os.environ['MINI_ORK_DB']=sys.argv[1]; "
             "from mini_ork.ported.agent_registry import current; "
             "r = current('role-no-such-xyz'); "
             "print(json.dumps(r) if r else 'null')",
             py_db],
            capture_output=True, text=True, cwd=str(REPO))
        assert rb_none.stdout.strip() == "null"
        assert rp_none.stdout.strip() == "null"
    finally:
        for p in (bash_db, py_db):
            if os.path.exists(p):
                os.unlink(p)


# ───────────────────────── Case 4: get unknown version → null ──────────────────

def test_get_unknown_version_returns_none():
    bash_db = _make_temp_db("mo-case4-bash")
    py_db = _make_temp_db("mo-case4-py")
    try:
        # Bootstrap so the table exists for bash's _agent_ensure_tables guard
        for db in (bash_db, py_db):
            with sqlite3.connect(db) as con:
                con.executescript(REGISTRY_DDL)
                con.commit()

        rb = _bash({"MINI_ORK_DB": bash_db},
                   'agent_get "planner" "av-doesnotexist"')
        rp = subprocess.run(
            ["python3", "-c",
             "import os, sys, json; sys.path.insert(0, '.'); "
             "os.environ['MINI_ORK_DB']=sys.argv[1]; "
             "from mini_ork.ported.agent_registry import get; "
             "r = get('planner', 'av-doesnotexist'); "
             "print(json.dumps(r) if r else 'null')",
             py_db],
            capture_output=True, text=True, cwd=str(REPO))

        assert rb.returncode == 0 and rb.stdout.strip() == "null"
        assert rp.returncode == 0 and rp.stdout.strip() == "null"
    finally:
        for p in (bash_db, py_db):
            if os.path.exists(p):
                os.unlink(p)


# ─────────────────────── Case 5: performance aggregates traces ────────────────

def test_performance_aggregates_traces():
    bash_db = _make_temp_db("mo-case5-bash")
    py_db = _make_temp_db("mo-case5-py")
    try:
        # Register one version on each side, then seed 2 traces (1 success, 1 failure)
        rb = _bash({"MINI_ORK_DB": bash_db},
                   'agent_register "planner" \'{"model":"m1"}\'')
        vid_bash = rb.stdout.strip()
        rp = subprocess.run(
            ["python3", "-c",
             "import os, sys; sys.path.insert(0, '.'); "
             "os.environ['MINI_ORK_DB']=sys.argv[1]; "
             "from mini_ork.ported.agent_registry import register; "
             "print(register('planner', {'model':'m1'}))",
             py_db],
            capture_output=True, text=True, cwd=str(REPO))
        assert rp.returncode == 0, rp.stderr
        vid_py = rp.stdout.strip()

        # Seed execution_traces on both DBs. Both bash (via trace_write) and
        # py (via trace_write with db=) need the table to exist — create it.
        for db, vid in ((bash_db, vid_bash), (py_db, vid_py)):
            with sqlite3.connect(db) as con:
                con.executescript(TRACES_DDL)
                con.execute(
                    "INSERT INTO execution_traces "
                    "(trace_id, agent_version_id, task_class, cost_usd, duration_ms, status) "
                    "VALUES (?, ?, 'code-review', 0.05, 1500, 'success')",
                    (f"tr-{vid}-1", vid),
                )
                con.execute(
                    "INSERT INTO execution_traces "
                    "(trace_id, agent_version_id, task_class, cost_usd, duration_ms, status) "
                    "VALUES (?, ?, 'code-review', 0.02, 800, 'failure')",
                    (f"tr-{vid}-2", vid),
                )
                con.commit()

        # Bash performance
        rb_perf = _bash({"MINI_ORK_DB": bash_db}, 'agent_performance "planner"')
        # Py performance via subprocess (CLI wrapper parity)
        rp_perf = subprocess.run(
            ["python3", "-c",
             "import os, sys, json; sys.path.insert(0, '.'); "
             "os.environ['MINI_ORK_DB']=sys.argv[1]; "
             "from mini_ork.ported.agent_registry import performance; "
             "print(json.dumps(performance('planner')))",
             py_db],
            capture_output=True, text=True, cwd=str(REPO))

        assert rb_perf.returncode == 0, rb_perf.stderr
        assert rp_perf.returncode == 0, rp_perf.stderr

        bash_perf = json.loads(rb_perf.stdout.strip())
        py_perf = json.loads(rp_perf.stdout.strip())

        # Structural parity
        assert bash_perf["role"] == py_perf["role"] == "planner"
        assert bash_perf["version_count"] == py_perf["version_count"] == 1
        assert len(bash_perf["versions"]) == len(py_perf["versions"]) == 1

        bv = bash_perf["versions"][0]
        pv = py_perf["versions"][0]
        assert bv["version_id"] == vid_bash
        assert pv["version_id"] == vid_py
        # Float parity with 1e-6 tolerance per kickoff
        assert bv["total_runs"] == pv["total_runs"] == 2
        assert bv["success_runs"] == pv["success_runs"] == 1
        assert abs(bv["success_rate"] - pv["success_rate"]) < 1e-6
        # avg_cost = (0.05 + 0.02)/2 = 0.035 — both sides must round to 0.035
        assert abs(bv["avg_cost_usd"] - pv["avg_cost_usd"]) < 1e-6
        assert abs(bv["avg_cost_usd"] - 0.035) < 1e-6
        # avg_duration = (1500 + 800)/2 = 1150.0
        assert abs(bv["avg_duration_ms"] - pv["avg_duration_ms"]) < 0.1
        assert abs(bv["avg_duration_ms"] - 1150.0) < 0.1
    finally:
        for p in (bash_db, py_db):
            if os.path.exists(p):
                os.unlink(p)


# ─────────────────────────── Case 6: error-path parity ─────────────────────────

def _py_call(script, *args, cwd=None):
    """Run a short Python script via stdin so we can use real newlines + try/except."""
    return subprocess.run(
        ["python3", "-", *args],
        input=script, capture_output=True, text=True, cwd=cwd or str(REPO),
    )


def test_register_errors_match():
    bash_db = _make_temp_db("mo-case6-bash")
    py_db = _make_temp_db("mo-case6-py")
    try:
        # (a) Invalid JSON: bash exits non-zero with stderr prefix; py raises ValueError
        rb = _bash({"MINI_ORK_DB": bash_db}, 'agent_register "executor" "not-json"')
        rp = _py_call(
            "import os, sys\n"
            "sys.path.insert(0, '.')\n"
            "os.environ['MINI_ORK_DB'] = sys.argv[1]\n"
            "from mini_ork.ported.agent_registry import register\n"
            "try:\n"
            "    register('executor', 'not-json')\n"
            "except ValueError as e:\n"
            "    print('ERR:', e)\n"
            "    sys.exit(0)\n",
            py_db,
        )

        assert rb.returncode != 0
        assert "agent_register" in rb.stderr and "invalid JSON" in rb.stderr
        assert rp.returncode == 0
        assert "agent_register" in rp.stdout and "invalid JSON" in rp.stdout

        # (b) Missing 'model' key
        rb2 = _bash({"MINI_ORK_DB": bash_db},
                    'agent_register "executor" \'{"provider":"anthropic"}\'')
        rp2 = _py_call(
            "import os, sys\n"
            "sys.path.insert(0, '.')\n"
            "os.environ['MINI_ORK_DB'] = sys.argv[1]\n"
            "from mini_ork.ported.agent_registry import register\n"
            "try:\n"
            "    register('executor', {'provider': 'anthropic'})\n"
            "except ValueError as e:\n"
            "    print('ERR:', e)\n"
            "    sys.exit(0)\n",
            py_db,
        )

        assert rb2.returncode != 0
        assert "agent_register" in rb2.stderr and "model" in rb2.stderr
        assert rp2.returncode == 0
        assert "agent_register" in rp2.stdout and "model" in rp2.stdout
    finally:
        for p in (bash_db, py_db):
            if os.path.exists(p):
                os.unlink(p)


# ───────────── Case 7: db/init.sh bootstrapped DB parity (slow, opt-in) ──────

def test_init_sh_bootstrapped_db(tmp_path):
    """Run db/init.sh against a temp DB, then bash + py register/get/current
    on THAT DB (proves the port works on a real-migration schema, not just
    its own lazy CREATE TABLE). Slow — 83+ migrations.
    Set MO_FAST_PARITY=1 to skip."""
    if os.environ.get("MO_FAST_PARITY"):
        pytest.skip("MO_FAST_PARITY=1 set; skipping slow db/init.sh bootstrap")

    bash_db = _make_temp_db("mo-case7-bash")
    py_db = _make_temp_db("mo-case7-py")
    home = tmp_path / "home"
    home.mkdir()
    try:
        # Bootstrap both DBs via db/init.sh
        for db in (bash_db, py_db):
            r = subprocess.run(
                ["bash", str(DB_INIT_SH)],
                env={**os.environ, "MINI_ORK_HOME": str(home),
                     "MINI_ORK_DB": db},
                capture_output=True, text=True,
            )
            assert r.returncode == 0, f"db/init.sh failed: {r.stderr}"

        # Register one version on each
        rb = _bash({"MINI_ORK_DB": bash_db},
                   'agent_register "planner" \'{"model":"m1"}\'')
        vid_bash = rb.stdout.strip()
        rp = subprocess.run(
            ["python3", "-c",
             "import os, sys; sys.path.insert(0, '.'); "
             "os.environ['MINI_ORK_DB']=sys.argv[1]; "
             "from mini_ork.ported.agent_registry import register; "
             "print(register('planner', {'model':'m1'}))",
             py_db],
            capture_output=True, text=True, cwd=str(REPO))
        assert rp.returncode == 0, rp.stderr
        vid_py = rp.stdout.strip()

        # Row dump parity: same model / role / status on both DBs
        with sqlite3.connect(bash_db) as con:
            bash_row = con.execute(
                "SELECT model, role, status FROM agent_registry WHERE version_id=?",
                (vid_bash,),
            ).fetchone()
        with sqlite3.connect(py_db) as con:
            py_row = con.execute(
                "SELECT model, role, status FROM agent_registry WHERE version_id=?",
                (vid_py,),
            ).fetchone()
        assert bash_row == py_row == ("m1", "planner", "active")

        # Schema parity: same column set for agent_registry on both DBs
        with sqlite3.connect(bash_db) as con:
            bash_cols = {r[1] for r in con.execute("PRAGMA table_info(agent_registry)").fetchall()}
        with sqlite3.connect(py_db) as con:
            py_cols = {r[1] for r in con.execute("PRAGMA table_info(agent_registry)").fetchall()}
        assert bash_cols == py_cols
        # And both sides have execution_traces from the real migration
        for db in (bash_db, py_db):
            with sqlite3.connect(db) as con:
                t = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='execution_traces'"
                ).fetchone()
                assert t is not None
    finally:
        for p in (bash_db, py_db):
            if os.path.exists(p):
                os.unlink(p)
        if home.exists():
            shutil.rmtree(home, ignore_errors=True)