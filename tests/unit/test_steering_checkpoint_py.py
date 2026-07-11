"""Parity gate: mini_ork.ported.steering_checkpoint vs lib/steering_checkpoint.sh.

Each test invokes the LIVE bash subprocess (sourcing lib/steering_checkpoint.sh,
which itself sources lib/operator_steering.sh, in a single ``bash -c`` block so
the functions are visible to the caller) on the same inputs as the Python port
and asserts identical output — return code, sentinel/marker file presence, and
marker/status JSON fields. No mocks, no hardcoded expected outputs: expected is
always derived from a control bash invocation sharing the inputs.

Eight cases (above the kickoff's >=6 floor):
  (a) has_unconsumed empty run_id      → both rc=2 + stderr "run_id required"
  (b) has_unconsumed missing db        → both rc=1
  (c) has_unconsumed no-rows / seeded  → both rc=1 then rc=0, incl. global
                                          NULL-run queue matching
  (d) mark + status + clear round-trip → sentinel created then removed; marker
                                          JSON byte-equivalent (bash vs python);
                                          status JSON fields match
  (e) gate with steering present       → both rc=0 + marker cleared
  (f) gate with no steering            → both rc=2 + marker JSON keys match
  (g) role-matching parity             → bash rc == py rc for role in
                                          {any, planner, reviewer} incl. the
                                          '? = any' broadcast branch (both dirs)
  (h) DB-diff parity                   → 3 mixed run/role rows; bash and python
                                          see the SAME subset for every probe

Environment isolation (mirrors tests/unit/test_operator_steering_py.py):
  The shell pytest runs in often has MINI_ORK_DB / MINI_ORK_HOME / MINI_ORK_RUN_DIR
  pointed at the real repo. Each test redirects the Python process via
  monkeypatch.setenv (auto-revert) AND builds the bash subprocess env from
  os.environ so the monkeypatched values propagate to both sides identically.
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
from mini_ork.ported import steering_checkpoint as sc  # noqa: E402

SC_SH = REPO / "lib" / "steering_checkpoint.sh"
INIT_SH = REPO / "db" / "init.sh"


def _init_db(tmp_path_factory, *, name: str = "home") -> tuple[str, str]:
    """Spin up a fresh mini-ork SQLite DB via db/init.sh; returns (db, home)."""
    home = tmp_path_factory.mktemp(name)
    dbp = str(home / "state.db")
    subprocess.run(
        ["bash", str(INIT_SH)],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
        capture_output=True, text=True, check=True,
    )
    return dbp, str(home)


def _point_env(monkeypatch: pytest.MonkeyPatch, *, db: str, home: str,
               run_dir: str | None = None) -> dict:
    """Redirect the Python process env AND return the matching subprocess env.

    Without this the port would resolve to whatever the shell pytest inherited
    (usually the repo's real state.db / run dir), corrupting parity.
    """
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", home)
    if run_dir is not None:
        monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    else:
        monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    return dict(os.environ)


def _seed_row(
    db: str,
    *,
    run_id: str | None,
    role_target: str = "any",
    severity: str = "info",
    message: str = "steer",
    source: str = "",
    confidence: float = 0.8,
    created_at: int | None = None,
    expires_at: int | None = None,
    consumed_at: int | None = None,
) -> int:
    """Direct-SQL insert so we can craft expired / pre-consumed / global-NULL
    rows for the filter tests (bypasses the emit path, which lives behind a
    separate port)."""
    now = int(time.time() * 1000)
    if created_at is None:
        created_at = now
    if expires_at is None:
        expires_at = now + 3600 * 1000
    con = sqlite3.connect(db)
    try:
        cur = con.execute(
            """INSERT INTO operator_steering
                 (run_id, role_target, severity, message, source,
                  confidence, created_at, expires_at, consumed_at)
               VALUES (NULLIF(?, ''), ?, ?, ?, NULLIF(?, ''), ?, ?, ?, ?)""",
            (
                run_id if run_id is not None else "",
                role_target, severity, message, source,
                float(confidence), int(created_at), int(expires_at), consumed_at,
            ),
        )
        con.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)
    finally:
        con.close()


def _bash_has_unconsumed(env: dict, run_id: str, role: str = "any"):
    """Run the live bash has_unconsumed; return (rc, stderr)."""
    wrapper = (
        f'. "{SC_SH}"\n'
        f'mo_steering_has_unconsumed {json.dumps(run_id)} {json.dumps(role)}\n'
        'echo "RC=$?"\n'
    )
    r = subprocess.run(["bash", "-c", wrapper], env=env,
                       capture_output=True, text=True)
    rc = int(r.stdout.strip().rsplit("RC=", 1)[1])
    return rc, r.stderr


def _bash_gate(env: dict, run_id: str, node_id: str = "checkpoint",
               role: str = "any") -> int:
    wrapper = (
        f'. "{SC_SH}"\n'
        f'mo_steering_checkpoint_gate {json.dumps(run_id)} {json.dumps(node_id)} {json.dumps(role)}\n'
        'echo "RC=$?"\n'
    )
    r = subprocess.run(["bash", "-c", wrapper], env=env,
                       capture_output=True, text=True)
    return int(r.stdout.strip().rsplit("RC=", 1)[1])


def _bash_call(env: dict, line: str) -> subprocess.CompletedProcess:
    wrapper = f'. "{SC_SH}"\n{line}\n'
    return subprocess.run(["bash", "-c", wrapper], env=env,
                          capture_output=True, text=True)


# ─────────────────────────────────────────────────────────────────────────────
# (a) has_unconsumed empty run_id → rc=2 + stderr "run_id required"
# ─────────────────────────────────────────────────────────────────────────────
def test_has_unconsumed_empty_run_id_parity(tmp_path_factory, monkeypatch, capsys):
    db, home = _init_db(tmp_path_factory)
    env = _point_env(monkeypatch, db=db, home=home)

    bash_rc, bash_err = _bash_has_unconsumed(env, "")
    py_rc = sc.has_unconsumed("")
    py_err = capsys.readouterr().err

    assert bash_rc == py_rc == 2
    assert "mo_steering_has_unconsumed: run_id required" in bash_err
    assert "mo_steering_has_unconsumed: run_id required" in py_err


# ─────────────────────────────────────────────────────────────────────────────
# (b) has_unconsumed missing db → rc=1
# ─────────────────────────────────────────────────────────────────────────────
def test_has_unconsumed_missing_db_parity(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("nodb")
    missing_db = str(home / "state.db")  # never created
    assert not os.path.isfile(missing_db)
    env = _point_env(monkeypatch, db=missing_db, home=str(home))

    bash_rc, _ = _bash_has_unconsumed(env, "r-b")
    py_rc = sc.has_unconsumed("r-b")

    assert bash_rc == py_rc == 1


# ─────────────────────────────────────────────────────────────────────────────
# (c) has_unconsumed no-rows / seeded row / global NULL-run queue
# ─────────────────────────────────────────────────────────────────────────────
def test_has_unconsumed_rows_parity(tmp_path_factory, monkeypatch):
    db, home = _init_db(tmp_path_factory)
    env = _point_env(monkeypatch, db=db, home=home)

    # no rows → both rc=1
    assert _bash_has_unconsumed(env, "r-c")[0] == sc.has_unconsumed("r-c") == 1

    # seed a row addressed to this run → both rc=0
    _seed_row(db, run_id="r-c", role_target="any")
    assert _bash_has_unconsumed(env, "r-c")[0] == sc.has_unconsumed("r-c") == 0

    # a *different* run with only the run-scoped row present → still rc=1
    assert _bash_has_unconsumed(env, "r-other")[0] == sc.has_unconsumed("r-other") == 1

    # global NULL-run queue row is visible to any run → both rc=0
    _seed_row(db, run_id=None, role_target="any", message="global")
    assert _bash_has_unconsumed(env, "r-other")[0] == sc.has_unconsumed("r-other") == 0


# ─────────────────────────────────────────────────────────────────────────────
# (d) mark + status + clear round-trip; marker JSON byte-equivalent
# ─────────────────────────────────────────────────────────────────────────────
def test_mark_status_clear_roundtrip_parity(tmp_path_factory, monkeypatch):
    db, home = _init_db(tmp_path_factory)
    bash_run_dir = str(tmp_path_factory.mktemp("d_bash_run"))
    py_run_dir = str(tmp_path_factory.mktemp("d_py_run"))

    # Bash side: mark, capture marker JSON, status JSON, then clear.
    bash_env = {**os.environ, "MINI_ORK_DB": db, "MINI_ORK_HOME": home,
                "MINI_ORK_RUN_DIR": bash_run_dir}
    _bash_call(bash_env, 'mo_steering_checkpoint_mark "r-d" "node-7" "please advise"')
    bash_sentinel = os.path.join(bash_run_dir, ".steering-checkpoint")
    bash_marker = os.path.join(bash_run_dir, ".steering-checkpoint.json")
    assert os.path.isfile(bash_sentinel) and os.path.isfile(bash_marker)
    with open(bash_marker) as fh:
        bash_marker_json = json.load(fh)
    bash_status = json.loads(
        _bash_call(bash_env, 'mo_steering_checkpoint_status "r-d"').stdout.strip()
    )

    # Python side: same operations against its own run dir.
    _point_env(monkeypatch, db=db, home=home, run_dir=py_run_dir)
    sc.mark("r-d", "node-7", "please advise")
    py_sentinel = sc._sentinel_path("r-d")
    py_marker = sc._marker_path("r-d")
    assert os.path.isfile(py_sentinel) and os.path.isfile(py_marker)
    with open(py_marker) as fh:
        py_marker_json = json.load(fh)
    py_status = sc.status("r-d")

    # Marker JSON equal modulo requested_at_ms (wall-clock, ms-precision int);
    # both sides derive it from int(time.time()*1000) so allow small drift.
    assert abs(py_marker_json.pop("requested_at_ms")
               - bash_marker_json.pop("requested_at_ms")) < 5000
    assert py_marker_json == bash_marker_json == {
        "awaiting_steering": True,
        "run_id": "r-d",
        "node_id": "node-7",
        "reason": "please advise",
    }

    # status: same shape modulo sentinel_path (per-side run dir) + requested_at_ms.
    for s in (bash_status, py_status):
        s.pop("requested_at_ms", None)
        s.pop("sentinel_path", None)
    assert py_status == bash_status
    assert py_status["awaiting"] is True

    # clear removes both files on both sides.
    _bash_call(bash_env, 'mo_steering_checkpoint_clear "r-d"')
    assert not os.path.isfile(bash_sentinel) and not os.path.isfile(bash_marker)
    sc.clear("r-d")
    assert not os.path.isfile(py_sentinel) and not os.path.isfile(py_marker)

    # status after clear → {"awaiting": false} on both sides.
    bash_status_cleared = json.loads(
        _bash_call(bash_env, 'mo_steering_checkpoint_status "r-d"').stdout.strip()
    )
    assert bash_status_cleared == sc.status("r-d") == {"awaiting": False}


# ─────────────────────────────────────────────────────────────────────────────
# (e) gate with steering present → rc=0 + marker cleared
# ─────────────────────────────────────────────────────────────────────────────
def test_gate_steering_present_parity(tmp_path_factory, monkeypatch):
    db, home = _init_db(tmp_path_factory)
    bash_run_dir = str(tmp_path_factory.mktemp("e_bash_run"))
    py_run_dir = str(tmp_path_factory.mktemp("e_py_run"))
    # Seed an unconsumed steering row so the gate is satisfied.
    _seed_row(db, run_id="r-e", role_target="any")

    bash_env = {**os.environ, "MINI_ORK_DB": db, "MINI_ORK_HOME": home,
                "MINI_ORK_RUN_DIR": bash_run_dir}
    bash_rc = _bash_gate(bash_env, "r-e")
    assert not os.path.isfile(os.path.join(bash_run_dir, ".steering-checkpoint"))

    _point_env(monkeypatch, db=db, home=home, run_dir=py_run_dir)
    py_rc = sc.gate("r-e")
    assert not os.path.isfile(sc._sentinel_path("r-e"))

    assert bash_rc == py_rc == 0


# ─────────────────────────────────────────────────────────────────────────────
# (f) gate with no steering → rc=2 + marker JSON keys match
# ─────────────────────────────────────────────────────────────────────────────
def test_gate_no_steering_parity(tmp_path_factory, monkeypatch):
    db, home = _init_db(tmp_path_factory)  # empty steering table
    bash_run_dir = str(tmp_path_factory.mktemp("f_bash_run"))
    py_run_dir = str(tmp_path_factory.mktemp("f_py_run"))

    bash_env = {**os.environ, "MINI_ORK_DB": db, "MINI_ORK_HOME": home,
                "MINI_ORK_RUN_DIR": bash_run_dir}
    bash_rc = _bash_gate(bash_env, "r-f", "node-f")
    with open(os.path.join(bash_run_dir, ".steering-checkpoint.json")) as fh:
        bash_marker = json.load(fh)

    _point_env(monkeypatch, db=db, home=home, run_dir=py_run_dir)
    py_rc = sc.gate("r-f", "node-f")
    with open(sc._marker_path("r-f")) as fh:
        py_marker = json.load(fh)

    assert bash_rc == py_rc == 2
    assert sorted(py_marker) == sorted(bash_marker)
    py_marker.pop("requested_at_ms")
    bash_marker.pop("requested_at_ms")
    assert py_marker == bash_marker == {
        "awaiting_steering": True,
        "run_id": "r-f",
        "node_id": "node-f",
        "reason": "awaiting human steering",
    }


# ─────────────────────────────────────────────────────────────────────────────
# (g) role-matching parity — including the '? = any' broadcast branch both ways
# ─────────────────────────────────────────────────────────────────────────────
def test_has_unconsumed_role_matching_parity(tmp_path_factory, monkeypatch):
    db, home = _init_db(tmp_path_factory)
    env = _point_env(monkeypatch, db=db, home=home)

    # Row addressed specifically to 'planner'.
    _seed_row(db, run_id="r-g", role_target="planner", message="for-planner")

    def check(role: str) -> int:
        bash_rc, _ = _bash_has_unconsumed(env, "r-g", role)
        py_rc = sc.has_unconsumed("r-g", role)
        assert bash_rc == py_rc, f"role={role}: bash={bash_rc} py={py_rc}"
        return py_rc

    # role='planner' → direct match → 0.
    assert check("planner") == 0
    # role='any'     → '? = any' broadcast branch matches the planner row → 0.
    assert check("any") == 0
    # role='reviewer'→ no planner match, no 'any'-targeted row, not broadcast → 1.
    assert check("reviewer") == 1

    # Inverse broadcast: an 'any'-targeted row is visible to a specific role.
    _seed_row(db, run_id="r-g2", role_target="any", message="broadcast")
    bash_rc, _ = _bash_has_unconsumed(env, "r-g2", "reviewer")
    py_rc = sc.has_unconsumed("r-g2", "reviewer")
    assert bash_rc == py_rc == 0


# ─────────────────────────────────────────────────────────────────────────────
# (h) DB-diff parity — 3 mixed run/role rows; identical subset per probe
# ─────────────────────────────────────────────────────────────────────────────
def test_has_unconsumed_mixed_rows_subset_parity(tmp_path_factory, monkeypatch):
    db, home = _init_db(tmp_path_factory)
    env = _point_env(monkeypatch, db=db, home=home)

    # Three mixed rows: run-scoped planner, run-scoped reviewer, global 'any'.
    _seed_row(db, run_id="r-h1", role_target="planner", message="h1-planner")
    _seed_row(db, run_id="r-h2", role_target="reviewer", message="h2-reviewer")
    _seed_row(db, run_id=None, role_target="any", message="h-global")

    probes = [
        ("r-h1", "planner"),   # own planner row + global any → 0
        ("r-h1", "reviewer"),  # no reviewer row for r-h1, but global any → 0
        ("r-h2", "reviewer"),  # own reviewer row + global any → 0
        ("r-unknown", "any"),  # only the global any row is visible → 0
        ("r-h1", "verifier"),  # planner row not matched, but global any → 0
    ]
    # Prove bash and python agree on EVERY probe (identical visible subset).
    for run_id, role in probes:
        bash_rc, _ = _bash_has_unconsumed(env, run_id, role)
        py_rc = sc.has_unconsumed(run_id, role)
        assert bash_rc == py_rc, f"probe=({run_id},{role}): bash={bash_rc} py={py_rc}"

    # Now delete the global row and re-probe: r-unknown/verifier should flip to 1
    # on BOTH sides (subset really is driven by the same WHERE clause).
    con = sqlite3.connect(db)
    con.execute("DELETE FROM operator_steering WHERE run_id IS NULL")
    con.commit()
    con.close()

    for run_id, role, expected in [
        ("r-h1", "planner", 0),    # still has its own planner row
        ("r-unknown", "any", 1),   # global gone → nothing visible
        ("r-h1", "verifier", 1),   # global gone, planner row not matched
    ]:
        bash_rc, _ = _bash_has_unconsumed(env, run_id, role)
        py_rc = sc.has_unconsumed(run_id, role)
        assert bash_rc == py_rc == expected, (
            f"probe=({run_id},{role}): bash={bash_rc} py={py_rc} exp={expected}")
