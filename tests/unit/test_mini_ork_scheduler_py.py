"""Parity gate: mini_ork.ported.mini_ork_scheduler vs bin/mini-ork-scheduler.

Tests the deterministic surface (no real dispatch): priority-aware pick, --once
--dry-run (pick + no status change), the 24h budget cap, empty-queue, and
missing-db — all vs the LIVE bash on a seeded state.db. recipes/epic-runner in
the real repo satisfies the recipe preflight.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_scheduler as sch  # noqa: E402

BIN = REPO / "bin" / "mini-ork-scheduler"


def _sql(db, stmt):
    return subprocess.run(["sqlite3", db, stmt], capture_output=True, text=True).stdout.strip()


@pytest.fixture
def db(tmp_path):
    home = tmp_path / ".mini-ork"; home.mkdir()
    d = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": d},
                   capture_output=True, text=True, check=True)
    _sql(d, "ALTER TABLE epics ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;")
    return d, str(home)


def _seed(db, eid, status="not started", pri=0, created="2026-01-01T00:00:00Z"):
    _sql(db, f"INSERT INTO epics (id,title,status,priority,created_at) "
             f"VALUES ('{eid}','{eid}','{status}',{pri},'{created}');")


def _env(home, db):
    return {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": home, "MINI_ORK_DB": db}


def test_pick_highest_priority(db):
    d, _ = db
    _seed(d, "low", pri=1); _seed(d, "high", pri=5); _seed(d, "mid", pri=3)
    assert sch.pick_next_epic(d) == "high"


def test_pick_tie_oldest_first(db):
    d, _ = db
    _seed(d, "newer", pri=2, created="2026-02-01T00:00:00Z")
    _seed(d, "older", pri=2, created="2026-01-01T00:00:00Z")
    assert sch.pick_next_epic(d) == "older"


def test_dry_run_once_parity(db):
    d, home = db
    _seed(d, "e1", pri=5); _seed(d, "e2", pri=1)
    ob = subprocess.run(["bash", str(BIN), "--once", "--dry-run"],
                        capture_output=True, text=True, env=_env(home, d)).stdout
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = sch.main(["--once", "--dry-run"], db=d, root=str(REPO))
    def picked(s):
        m = re.search(r"next=(\S+)", s); return m.group(1) if m else None
    assert picked(ob) == picked(buf.getvalue()) == "e1"
    assert rc == 0
    # dry-run leaves it not-started on both
    assert _sql(d, "SELECT status FROM epics WHERE id='e1';") == "not started"


def test_budget_cap_parity(db):
    d, home = db
    _seed(d, "e1", pri=5)
    # a recent task_run over the cap
    _sql(d, "INSERT INTO task_runs (id, task_class, recipe, workflow_version, kickoff_path, status, "
            "cost_usd, created_at, updated_at) VALUES ('r1','x',NULL,'latest','k','classified',99.0,"
            "strftime('%s','now'),strftime('%s','now'));")
    rb = subprocess.run(["bash", str(BIN), "--once", "--budget-cap-usd", "10"],
                        capture_output=True, text=True, env=_env(home, d)).returncode
    rp = sch.main(["--once", "--budget-cap-usd", "10"], db=d, root=str(REPO))
    assert rb == rp == 2


def test_empty_queue_once(db):
    d, home = db
    _seed(d, "done1", status="done", pri=5)   # nothing ready
    rb = subprocess.run(["bash", str(BIN), "--once"], capture_output=True, text=True,
                        env=_env(home, d)).returncode
    rp = sch.main(["--once"], db=d, root=str(REPO))
    assert rb == rp == 0


def test_missing_db(tmp_path):
    missing = str(tmp_path / "nope.db")
    rb = subprocess.run(["bash", str(BIN), "--once"], capture_output=True, text=True,
                        env={**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_DB": missing,
                             "MINI_ORK_HOME": str(tmp_path)}).returncode
    rp = sch.main(["--once"], db=missing, root=str(REPO))
    assert rb == rp == 1
