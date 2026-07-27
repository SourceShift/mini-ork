"""Standalone unit tests for ``mini_ork.steering.steering_checkpoint``.

Replaces the bash-parity gate (against ``lib/steering_checkpoint.sh``) as
part of the bash→Python migration: the Python port is now the sole
implementation, so its coverage no longer runs the LIVE bash subprocess —
it asserts the port's behaviour directly. The expected values below are
the semantic contract the bash side used to pin (rc semantics, sentinel/
marker file lifecycle, marker/status JSON fields, role-matching and
filter clauses), now asserted on the port's output.

Cases:
  (a) has_unconsumed empty run_id      → rc=2 + stderr "run_id required"
  (b) has_unconsumed missing db        → rc=1
  (c) has_unconsumed no-rows / seeded  → rc=1 then rc=0, incl. global
                                          NULL-run queue matching
  (d) mark + status + clear round-trip → sentinel created then removed;
                                          marker + status JSON fields
  (e) gate with steering present       → rc=0 + marker cleared
  (f) gate with no steering            → rc=2 + marker JSON fields
  (g) role-matching                    → role in {any, planner, reviewer}
                                          incl. the '? = any' broadcast
                                          branch (both dirs)
  (h) mixed-row subset                 → 3 mixed run/role rows; expected
                                          visibility per probe
  (i) expired row                      → not actionable (rc=1)
  (j) consumed row                     → not actionable (rc=1)

Environment isolation: the shell pytest runs in often has MINI_ORK_DB /
MINI_ORK_HOME / MINI_ORK_RUN_DIR pointed at the real repo. Each test
redirects the Python process via monkeypatch.setenv (auto-revert) to a
tmp DB seeded via ``mini_ork.stores.migrate.init_db``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.steering import steering_checkpoint as sc  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402


def _init_db(tmp_path_factory, *, name: str = "home") -> tuple[str, str]:
    """Spin up a fresh mini-ork SQLite DB via init_db; returns (db, home)."""
    home = tmp_path_factory.mktemp(name)
    dbp = str(home / "state.db")
    rc, out, err = mig.init_db(db=dbp, root=str(REPO))
    assert rc == 0, f"init_db failed:\n{out}\n{err}"
    return dbp, str(home)


def _point_env(monkeypatch: pytest.MonkeyPatch, *, db: str, home: str,
               run_dir: str | None = None) -> None:
    """Redirect the Python process env to the tmp DB/home/run_dir.

    Without this the port would resolve to whatever the shell pytest
    inherited (usually the repo's real state.db / run dir).
    """
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", home)
    if run_dir is not None:
        monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    else:
        monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)


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
    """Direct-SQL insert so we can craft expired / pre-consumed /
    global-NULL rows for the filter tests (bypasses the emit path, which
    lives behind a separate port)."""
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


# ─────────────────────────────────────────────────────────────────────────────
# (a) has_unconsumed empty run_id → rc=2 + stderr "run_id required"
# ─────────────────────────────────────────────────────────────────────────────
def test_has_unconsumed_empty_run_id(tmp_path_factory, monkeypatch, capsys):
    db, home = _init_db(tmp_path_factory)
    _point_env(monkeypatch, db=db, home=home)

    py_rc = sc.has_unconsumed("")
    py_err = capsys.readouterr().err

    assert py_rc == 2
    assert "mo_steering_has_unconsumed: run_id required" in py_err


# ─────────────────────────────────────────────────────────────────────────────
# (b) has_unconsumed missing db → rc=1
# ─────────────────────────────────────────────────────────────────────────────
def test_has_unconsumed_missing_db(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("nodb")
    missing_db = str(home / "state.db")  # never created
    assert not os.path.isfile(missing_db)
    _point_env(monkeypatch, db=missing_db, home=str(home))

    py_rc = sc.has_unconsumed("r-b")

    assert py_rc == 1


# ─────────────────────────────────────────────────────────────────────────────
# (c) has_unconsumed no-rows / seeded row / global NULL-run queue
# ─────────────────────────────────────────────────────────────────────────────
def test_has_unconsumed_rows(tmp_path_factory, monkeypatch):
    db, home = _init_db(tmp_path_factory)
    _point_env(monkeypatch, db=db, home=home)

    # no rows → rc=1
    assert sc.has_unconsumed("r-c") == 1

    # seed a row addressed to this run → rc=0
    _seed_row(db, run_id="r-c", role_target="any")
    assert sc.has_unconsumed("r-c") == 0

    # a *different* run with only the run-scoped row present → still rc=1
    assert sc.has_unconsumed("r-other") == 1

    # global NULL-run queue row is visible to any run → rc=0
    _seed_row(db, run_id=None, role_target="any", message="global")
    assert sc.has_unconsumed("r-other") == 0


# ─────────────────────────────────────────────────────────────────────────────
# (d) mark + status + clear round-trip; marker/status JSON fields
# ─────────────────────────────────────────────────────────────────────────────
def test_mark_status_clear_roundtrip(tmp_path_factory, monkeypatch):
    db, home = _init_db(tmp_path_factory)
    py_run_dir = str(tmp_path_factory.mktemp("d_py_run"))

    _point_env(monkeypatch, db=db, home=home, run_dir=py_run_dir)
    sc.mark("r-d", "node-7", "please advise")
    py_sentinel = sc._sentinel_path("r-d")
    py_marker = sc._marker_path("r-d")
    assert os.path.isfile(py_sentinel) and os.path.isfile(py_marker)
    with open(py_marker) as fh:
        py_marker_json = json.load(fh)
    py_status = sc.status("r-d")

    # Marker JSON: requested_at_ms is a wall-clock ms-precision int.
    requested = py_marker_json.pop("requested_at_ms")
    assert isinstance(requested, int)
    assert abs(requested - int(time.time() * 1000)) < 5000
    assert py_marker_json == {
        "awaiting_steering": True,
        "run_id": "r-d",
        "node_id": "node-7",
        "reason": "please advise",
    }

    # status: awaiting + marker fields + sentinel path.
    assert py_status["awaiting"] is True
    assert py_status["run_id"] == "r-d"
    assert py_status["node_id"] == "node-7"
    assert py_status["reason"] == "please advise"
    assert py_status["sentinel_path"] == py_sentinel

    # clear removes both files.
    sc.clear("r-d")
    assert not os.path.isfile(py_sentinel) and not os.path.isfile(py_marker)

    # status after clear → {"awaiting": false}.
    assert sc.status("r-d") == {"awaiting": False}


# ─────────────────────────────────────────────────────────────────────────────
# (e) gate with steering present → rc=0 + marker cleared
# ─────────────────────────────────────────────────────────────────────────────
def test_gate_steering_present(tmp_path_factory, monkeypatch):
    db, home = _init_db(tmp_path_factory)
    py_run_dir = str(tmp_path_factory.mktemp("e_py_run"))
    # Seed an unconsumed steering row so the gate is satisfied.
    _seed_row(db, run_id="r-e", role_target="any")

    _point_env(monkeypatch, db=db, home=home, run_dir=py_run_dir)
    py_rc = sc.gate("r-e")
    assert not os.path.isfile(sc._sentinel_path("r-e"))

    assert py_rc == 0


# ─────────────────────────────────────────────────────────────────────────────
# (f) gate with no steering → rc=2 + marker JSON fields
# ─────────────────────────────────────────────────────────────────────────────
def test_gate_no_steering(tmp_path_factory, monkeypatch):
    db, home = _init_db(tmp_path_factory)  # empty steering table
    py_run_dir = str(tmp_path_factory.mktemp("f_py_run"))

    _point_env(monkeypatch, db=db, home=home, run_dir=py_run_dir)
    py_rc = sc.gate("r-f", "node-f")
    with open(sc._marker_path("r-f")) as fh:
        py_marker = json.load(fh)

    assert py_rc == 2
    py_marker.pop("requested_at_ms")
    assert py_marker == {
        "awaiting_steering": True,
        "run_id": "r-f",
        "node_id": "node-f",
        "reason": "awaiting human steering",
    }


# ─────────────────────────────────────────────────────────────────────────────
# (g) role-matching — including the '? = any' broadcast branch both ways
# ─────────────────────────────────────────────────────────────────────────────
def test_has_unconsumed_role_matching(tmp_path_factory, monkeypatch):
    db, home = _init_db(tmp_path_factory)
    _point_env(monkeypatch, db=db, home=home)

    # Row addressed specifically to 'planner'.
    _seed_row(db, run_id="r-g", role_target="planner", message="for-planner")

    # role='planner' → direct match → 0.
    assert sc.has_unconsumed("r-g", "planner") == 0
    # role='any'     → '? = any' broadcast branch matches the planner row → 0.
    assert sc.has_unconsumed("r-g", "any") == 0
    # role='reviewer'→ no planner match, no 'any'-targeted row, not broadcast → 1.
    assert sc.has_unconsumed("r-g", "reviewer") == 1

    # Inverse broadcast: an 'any'-targeted row is visible to a specific role.
    _seed_row(db, run_id="r-g2", role_target="any", message="broadcast")
    assert sc.has_unconsumed("r-g2", "reviewer") == 0


# ─────────────────────────────────────────────────────────────────────────────
# (h) mixed-row subset — 3 mixed run/role rows; expected visibility per probe
# ─────────────────────────────────────────────────────────────────────────────
def test_has_unconsumed_mixed_rows_subset(tmp_path_factory, monkeypatch):
    db, home = _init_db(tmp_path_factory)
    _point_env(monkeypatch, db=db, home=home)

    # Three mixed rows: run-scoped planner, run-scoped reviewer, global 'any'.
    _seed_row(db, run_id="r-h1", role_target="planner", message="h1-planner")
    _seed_row(db, run_id="r-h2", role_target="reviewer", message="h2-reviewer")
    _seed_row(db, run_id=None, role_target="any", message="h-global")

    probes = [
        ("r-h1", "planner", 0),   # own planner row + global any
        ("r-h1", "reviewer", 0),  # no reviewer row for r-h1, but global any
        ("r-h2", "reviewer", 0),  # own reviewer row + global any
        ("r-unknown", "any", 0),  # only the global any row is visible
        ("r-h1", "verifier", 0),  # planner row not matched, but global any
    ]
    for run_id, role, expected in probes:
        rc = sc.has_unconsumed(run_id, role)
        assert rc == expected, f"probe=({run_id},{role}): rc={rc} exp={expected}"

    # Now delete the global row and re-probe: r-unknown/verifier flip to 1.
    con = sqlite3.connect(db)
    con.execute("DELETE FROM operator_steering WHERE run_id IS NULL")
    con.commit()
    con.close()

    for run_id, role, expected in [
        ("r-h1", "planner", 0),    # still has its own planner row
        ("r-unknown", "any", 1),   # global gone → nothing visible
        ("r-h1", "verifier", 1),   # global gone, planner row not matched
    ]:
        rc = sc.has_unconsumed(run_id, role)
        assert rc == expected, f"probe=({run_id},{role}): rc={rc} exp={expected}"


# ─────────────────────────────────────────────────────────────────────────────
# (i) expired row → not actionable (expires_at in the past; the `expires_at >
#     now` clause filters it) — mirrors tests/unit/test_steering_checkpoint.sh
#     case 4.
# ─────────────────────────────────────────────────────────────────────────────
def test_has_unconsumed_expired_row_ignored(tmp_path_factory, monkeypatch):
    """A steering row whose expires_at is in the past is filtered by the
    `expires_at > now` clause. The row is otherwise addressed to this run
    and unconsumed, so this isolates the expiry filter — rc=1 (not
    actionable)."""
    db, home = _init_db(tmp_path_factory)
    _point_env(monkeypatch, db=db, home=home)

    past = int(time.time() * 1000) - 1000  # 1s before now
    _seed_row(db, run_id="r-exp", role_target="any", expires_at=past)

    assert sc.has_unconsumed("r-exp") == 1


# ─────────────────────────────────────────────────────────────────────────────
# (j) consumed row → not actionable (consumed_at set; the `consumed_at IS
#     NULL` clause filters it) — mirrors tests/unit/test_steering_checkpoint.sh
#     case 5.
# ─────────────────────────────────────────────────────────────────────────────
def test_has_unconsumed_consumed_row_ignored(tmp_path_factory, monkeypatch):
    """A steering row that has already been consumed (consumed_at set) is
    filtered by the `consumed_at IS NULL` clause. expires_at stays in the
    future so this isolates the consumed filter — rc=1 (not actionable)."""
    db, home = _init_db(tmp_path_factory)
    _point_env(monkeypatch, db=db, home=home)

    now = int(time.time() * 1000)
    _seed_row(db, run_id="r-con", role_target="any", consumed_at=now)

    assert sc.has_unconsumed("r-con") == 1
