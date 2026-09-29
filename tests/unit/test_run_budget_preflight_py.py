"""`mini-ork run` refuses to start when the rolling-24h budget is spent.

Before this, the cost circuit tripped per LLM call inside the run: every node
failed in seconds with finish_reason cost_limit and the run ended as an
ordinary verdict=fail with files_changed 0 (observed: a framework-edit run and
the first held-out runner dispatch both "failed" this way at $0).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import main as cli_main  # noqa: E402


def _home_with_spend(tmp_path: Path, spent: float) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    db = home / "state.db"
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": str(db)},
                   capture_output=True, text=True, check=True)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO task_runs (id, task_class, kickoff_path, cost_usd, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)", ("run-old", "code_fix", "k.md", spent, int(time.time()) - 60, int(time.time()) - 60))
    con.commit()
    con.close()
    return home


def _run(tmp_path: Path, home: Path, budget: str) -> subprocess.CompletedProcess:
    kickoff = tmp_path / "k.md"
    kickoff.write_text("# fix\n\n## Files in scope\n\n- a.py\n")
    env = {**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": str(home / "state.db"),
           "MINI_ORK_ROOT": str(REPO), "MO_DAILY_BUDGET_USD": budget,
           "MINI_ORK_RUN_ID": "budget-probe", "MINI_ORK_DRY_RUN": "0",
           "MINI_ORK_PROFILE_GATE": "0", "MINI_ORK_NONINTERACTIVE": "1"}
    return subprocess.run([str(REPO / "bin" / "mini-ork"), "run", "--json", "code-fix", str(kickoff)],
                          capture_output=True, text=True, env=env, timeout=120)


def test_an_exhausted_budget_blocks_the_run_before_anything_dispatches(tmp_path):
    home = _home_with_spend(tmp_path, spent=12.5)

    r = _run(tmp_path, home, budget="10")

    assert r.returncode == cli_main.RC_BLOCKED, r.stderr[-500:]
    assert "daily budget exhausted" in r.stderr and "$12.50" in r.stderr
    result = json.loads(r.stdout.split("mini_ork_result=", 1)[1].splitlines()[0])
    assert result["verdict"] == "blocked" and result["blocked_reason"] == "cost_limit"
    con = sqlite3.connect(home / "state.db")
    traces = con.execute("SELECT COUNT(*) FROM execution_traces WHERE run_id='budget-probe'").fetchone()[0]
    con.close()
    assert traces == 0  # not one node was dispatched


def test_preflight_passes_under_budget(tmp_path, monkeypatch):
    home = _home_with_spend(tmp_path, spent=2.0)
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    monkeypatch.setenv("MO_DAILY_BUDGET_USD", "10")
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "0")
    sink: dict = {}

    assert cli_main._budget_preflight(sink) is None
    assert sink == {}


def test_dry_runs_are_never_blocked(tmp_path, monkeypatch):
    home = _home_with_spend(tmp_path, spent=99.0)
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    monkeypatch.setenv("MO_DAILY_BUDGET_USD", "10")
    monkeypatch.setenv("MINI_ORK_DRY_RUN", "1")

    assert cli_main._budget_preflight({}) is None
