"""Parity gate: mini_ork.orchestration.epic_graph vs lib/epic_graph.sh.

Seeds an epic DAG (A -> B hard, A -> C soft, blocked D), then runs the same
operations through the LIVE bash functions and the Python port on separate DB
copies, asserting identical ready-sets, deps_met, and on_done cascades.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.orchestration import epic_graph as eg

EG_SH = REPO / "lib" / "epic_graph.sh"


def _bash(db, snippet):
    r = subprocess.run(["bash", "-c", f'. "{EG_SH}" && {snippet}'],
                       env={**os.environ, "MINI_ORK_DB": db,
                            "MINI_ORK_ROOT": str(REPO)},
                       capture_output=True, text=True)
    return r.stdout.strip(), r.returncode


@pytest.fixture
def db(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    dbp = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
                   capture_output=True, text=True, check=True)
    con = sqlite3.connect(dbp)
    for i, (eid, status) in enumerate(
            [("A", "not started"), ("B", "blocked"), ("C", "not started"),
             ("D", "blocked")]):
        con.execute(
            "INSERT INTO epics (id, title, status, created_at) "
            "VALUES (?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now',?))",
            (eid, f"epic {eid}", status, f"+{i} seconds"))
    con.commit()
    con.close()
    return dbp


def _seed_deps(db):
    eg.add_dep("A", "B", "hard", db=db)   # B blocked on A
    eg.add_dep("A", "C", "soft", db=db)   # soft: does NOT block C
    eg.add_dep("B", "D", "hard", db=db)   # D blocked on B


def test_ready_set_parity(db):
    _seed_deps(db)
    py = eg.ready_now(db=db)
    bash_out, _ = _bash(db, "epic_graph_ready_now")
    assert py == bash_out.splitlines()
    # A ready (no deps), C ready (only a soft dep); B/D blocked-status anyway.
    assert py == ["A", "C"]


def test_deps_met_parity(db):
    _seed_deps(db)
    for eid in ("A", "B", "C", "D"):
        _, rc = _bash(db, f"epic_graph_deps_met {eid}")
        assert eg.deps_met(eid, db=db) == (rc == 0), eid
    assert eg.deps_met("B", db=db) is False   # unresolved hard dep
    assert eg.deps_met("C", db=db) is True    # soft dep never blocks


def test_on_done_cascade_parity(db):
    _seed_deps(db)
    db_bash = db + ".bash"
    shutil.copy(db, db_bash)
    eg.on_done("A", db=db)                      # python side
    _bash(db_bash, "epic_graph_on_done A")      # bash side

    def status(dbp, eid):
        con = sqlite3.connect(dbp)
        s = con.execute("SELECT status FROM epics WHERE id=?", (eid,)).fetchone()[0]
        con.close()
        return s

    # B's only hard dep resolved -> unblocked, on both sides. D still blocked.
    assert status(db, "B") == status(db_bash, "B") == "not started"
    assert status(db, "D") == status(db_bash, "D") == "blocked"
    assert eg.ready_now(db=db) == ["A", "B", "C"]


def test_mark_ready_idempotent(db):
    eg.mark_ready("D", db=db)
    assert "D" in eg.ready_now(db=db)
    eg.mark_ready("D", db=db)  # no-op on non-blocked
    assert eg.ready_now(db=db).count("D") == 1
