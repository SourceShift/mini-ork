"""Parity + behavior gate: mini_ork.scheduler vs bin/mini-ork-scheduler.

Parity: effective_priority + pick ordering are compared against the LIVE bash
functions (extracted verbatim from the script — sourcing it would run its main
loop). Behavior: dispatch/verdict/cascade with a stub runner, and the win #1
proof — three independent epics drain concurrently, not serially.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import scheduler  # noqa: E402
from mini_ork.ported import epic_graph as eg  # noqa: E402

SCHED = REPO / "bin" / "mini-ork-scheduler"


def _bash_fn(db, fn, *args):
    """Extract a function from the scheduler script and invoke it live."""
    snippet = subprocess.run(
        ["sed", "-n", f"/^{fn}()/,/^}}/p", str(SCHED)],
        capture_output=True, text=True).stdout
    assert snippet.strip(), f"could not extract {fn} from scheduler"
    r = subprocess.run(
        ["bash", "-c", snippet + f'\nSTATE_DB="$MINI_ORK_DB"\n{fn} "$@"', "_", *args],
        env={**os.environ, "MINI_ORK_DB": db, "STATE_DB": db},
        capture_output=True, text=True)
    return r.stdout.strip()


@pytest.fixture
def world(tmp_path_factory):
    """Temp root (kickoffs + stub runner) + schema'd DB with a priority DAG."""
    root = tmp_path_factory.mktemp("root")
    home = root / ".mini-ork"
    (home / "runs").mkdir(parents=True)
    dbp = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": dbp},
                   capture_output=True, text=True, check=True)
    scheduler.ensure_priority_column(dbp)
    con = sqlite3.connect(dbp)
    # A(prio 0) blocks B(prio 10); C,D,E independent (prio 5,0,0)
    for i, (eid, status, prio) in enumerate(
            [("A", "not started", 0), ("B", "blocked", 10),
             ("C", "not started", 5), ("D", "not started", 0),
             ("E", "not started", 0)]):
        con.execute(
            "INSERT INTO epics (id, title, status, priority, created_at) "
            "VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now',?))",
            (eid, f"epic {eid}", status, prio, f"+{i} seconds"))
    con.commit()
    con.close()
    eg.add_dep("A", "B", "hard", db=dbp)
    (root / "kickoffs").mkdir()
    for eid in "ABCDE":
        (root / "kickoffs" / f"{eid}.md").write_text(f"# epic {eid}\n")
    return {"root": str(root), "home": str(home), "db": dbp}


def _stub_runner(root, sleep_s=0.0, run_id="stubrun"):
    stub = Path(root) / "stub-runner.sh"
    stub.write_text(f"#!/bin/bash\nsleep {sleep_s}\necho run_id={run_id}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return str(stub)


def test_effective_priority_parity(world):
    db = world["db"]
    # A inherits blocked waiter B's priority 10; C keeps its own 5.
    for eid, expected in (("A", 10), ("C", 5), ("D", 0)):
        py = scheduler.effective_priority(eid, db=db)
        bash = int(_bash_fn(db, "_epic_effective_priority", eid) or 0)
        assert py == bash == expected, eid


def test_pick_ordering_parity(world):
    db = world["db"]
    ready = scheduler.pick_ready(db=db)
    bash_first = _bash_fn(db, "_pick_next_epic")
    # Inheritance puts A (eff 10) first, then C (5), then D/E oldest-first.
    assert ready == ["A", "C", "D", "E"]
    assert bash_first.splitlines()[0] == ready[0] == "A"


def test_dispatch_done_and_cascade(world):
    db, root, home = world["db"], world["root"], world["home"]
    runs = Path(home) / "runs" / "stubrun"
    runs.mkdir(parents=True)
    (runs / "verdict.json").write_text(json.dumps({"verdict": "success"}))
    verdict, rc = scheduler.dispatch_epic(
        "A", root, home, "epic-runner", db=db,
        runner_cmd=[_stub_runner(root)])
    assert verdict == "success" and rc == 0
    con = sqlite3.connect(db)
    a, b = [con.execute("SELECT status FROM epics WHERE id=?", (e,)).fetchone()[0]
            for e in ("A", "B")]
    con.close()
    assert a == "done"
    assert b == "not started"  # cascade unblocked the high-prio waiter


def test_pool_drains_concurrently_win1(world):
    db, root, home = world["db"], world["root"], world["home"]
    # Remove A/B so the pool sees exactly C,D,E (independent). Each stub run
    # sleeps 0.6s: serial = ~1.8s, pool(3) must finish well under that.
    con = sqlite3.connect(db)
    con.execute("DELETE FROM epics WHERE id IN ('A','B')")
    con.commit()
    con.close()
    t0 = time.time()
    n = scheduler.run_pool(root, home, db=db, max_parallel=3,
                           runner_cmd=[_stub_runner(root, sleep_s=0.6)])
    elapsed = time.time() - t0
    assert n == 3
    assert elapsed < 1.5, f"pool ran serially ({elapsed:.2f}s for 3x0.6s epics)"
