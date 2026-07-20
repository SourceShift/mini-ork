"""Standalone contracts for the canonical native epic scheduler."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork import scheduler  # noqa: E402
from mini_ork.orchestration import epic_graph as eg
from mini_ork.cli import epics

BIN = REPO / "bin" / "mini-ork-scheduler"


def _init_db(home: Path) -> str:
    home.mkdir(parents=True, exist_ok=True)
    db = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
        capture_output=True,
        text=True,
        check=True,
    )
    scheduler.ensure_priority_column(db)
    return db


def _seed(
    db: str,
    epic_id: str,
    *,
    status: str = "not started",
    priority: int = 0,
    created_at: str = "2026-01-01T00:00:00Z",
) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO epics (id,title,status,priority,created_at) VALUES (?,?,?,?,?)",
        (epic_id, epic_id, status, priority, created_at),
    )
    con.commit()
    con.close()


def _status(db: str, epic_id: str) -> str:
    con = sqlite3.connect(db)
    row = con.execute("SELECT status FROM epics WHERE id=?", (epic_id,)).fetchone()
    con.close()
    assert row is not None
    return str(row[0])


@pytest.fixture
def cli_world(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / ".mini-ork"
    return {"home": str(home), "db": _init_db(home)}


@pytest.fixture
def graph_world(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "root"
    home = root / ".mini-ork"
    (home / "runs").mkdir(parents=True)
    db = _init_db(home)
    for i, (epic_id, status, priority) in enumerate(
        [
            ("A", "not started", 0),
            ("B", "blocked", 10),
            ("C", "not started", 5),
            ("D", "not started", 0),
            ("E", "not started", 0),
        ]
    ):
        _seed(
            db,
            epic_id,
            status=status,
            priority=priority,
            created_at=f"2026-01-01T00:00:0{i}Z",
        )
    eg.add_dep("A", "B", "hard", db=db)
    (root / "kickoffs").mkdir()
    (root / "recipes" / "epic-runner").mkdir(parents=True)
    for epic_id in "ABCDE":
        (root / "kickoffs" / f"{epic_id}.md").write_text(
            f"# epic {epic_id}\n", encoding="utf-8"
        )
    return {"root": str(root), "home": str(home), "db": db}


def _stub_runner(root: str, sleep_s: float = 0.0, run_id: str = "stubrun") -> str:
    stub = Path(root) / "stub-runner.sh"
    stub.write_text(
        f"#!/bin/bash\nsleep {sleep_s}\necho run_id={run_id}\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return str(stub)


def _cli_env(world: dict[str, str]) -> dict[str, str]:
    return {
        **os.environ,
        "MINI_ORK_ROOT": str(REPO),
        "MINI_ORK_HOME": world["home"],
        "MINI_ORK_DB": world["db"],
    }


def test_public_launcher_has_one_native_owner() -> None:
    text = BIN.read_text(encoding="utf-8")
    assert os.access(BIN, os.X_OK)
    assert "from mini_ork.scheduler import main" in text
    assert "runtime-select.sh" not in text
    assert not (REPO / "mini_ork" / "ported" / "mini_ork_scheduler.py").exists()


def test_generated_verification_classifies_launcher_as_python() -> None:
    commands = epics._synth_verify(["bin/mini-ork-scheduler"])
    assert commands == ["python3 -m py_compile bin/mini-ork-scheduler"]


def test_effective_priority_and_ready_order_golden(graph_world: dict[str, str]) -> None:
    db = graph_world["db"]
    assert scheduler.effective_priority("A", db=db) == 10
    assert scheduler.effective_priority("C", db=db) == 5
    assert scheduler.effective_priority("D", db=db) == 0
    assert scheduler.pick_ready(db=db) == ["A", "C", "D", "E"]


def test_tied_priority_picks_oldest_first(cli_world: dict[str, str]) -> None:
    db = cli_world["db"]
    _seed(db, "newer", priority=2, created_at="2026-02-01T00:00:00Z")
    _seed(db, "older", priority=2, created_at="2026-01-01T00:00:00Z")
    assert scheduler.pick_ready(db=db) == ["older", "newer"]


def test_dispatch_done_and_cascade(graph_world: dict[str, str]) -> None:
    db, root, home = graph_world["db"], graph_world["root"], graph_world["home"]
    runs = Path(home) / "runs" / "stubrun"
    runs.mkdir(parents=True)
    (runs / "verdict.json").write_text(
        json.dumps({"verdict": "success"}), encoding="utf-8"
    )

    verdict, rc = scheduler.dispatch_epic(
        "A",
        root,
        home,
        "epic-runner",
        db=db,
        runner_cmd=[_stub_runner(root)],
    )

    assert (verdict, rc) == ("success", 0)
    assert _status(db, "A") == "done"
    assert _status(db, "B") == "not started"


def test_pool_drains_three_epics_concurrently(graph_world: dict[str, str]) -> None:
    db, root, home = graph_world["db"], graph_world["root"], graph_world["home"]
    con = sqlite3.connect(db)
    con.execute("DELETE FROM epic_dependencies")
    con.execute("DELETE FROM epics WHERE id IN ('A','B')")
    con.commit()
    con.close()

    started = time.monotonic()
    dispatched = scheduler.run_pool(
        root,
        home,
        db=db,
        max_parallel=3,
        runner_cmd=[_stub_runner(root, sleep_s=0.6)],
    )
    elapsed = time.monotonic() - started

    assert dispatched == 3
    assert elapsed < 1.5, f"pool ran serially ({elapsed:.2f}s for 3x0.6s epics)"


def test_main_activates_concurrent_pool(graph_world: dict[str, str]) -> None:
    db, root, home = graph_world["db"], graph_world["root"], graph_world["home"]
    con = sqlite3.connect(db)
    con.execute("DELETE FROM epic_dependencies")
    con.execute("DELETE FROM epics WHERE id IN ('A','B')")
    con.commit()
    con.close()

    started = time.monotonic()
    rc = scheduler.main(
        ["--max-iters", "3"],
        db=db,
        root=root,
        home=home,
        runner_cmd=[_stub_runner(root, sleep_s=0.6)],
    )
    elapsed = time.monotonic() - started

    assert rc == 3
    assert elapsed < 1.5, f"CLI main ran serially ({elapsed:.2f}s for 3x0.6s epics)"


def test_public_cli_once_dry_run_preserves_status(cli_world: dict[str, str]) -> None:
    db = cli_world["db"]
    _seed(db, "e1", priority=5)
    _seed(db, "e2", priority=1)

    result = subprocess.run(
        [str(BIN), "--once", "--dry-run"],
        capture_output=True,
        text=True,
        env=_cli_env(cli_world),
    )

    assert result.returncode == 0
    assert "next=e1" in result.stdout
    assert "would dispatch" in result.stdout
    assert _status(db, "e1") == "not started"


def test_direct_main_once_dry_run_uses_same_contract(cli_world: dict[str, str]) -> None:
    db = cli_world["db"]
    _seed(db, "e1", priority=5)
    output = io.StringIO()

    with redirect_stdout(output):
        rc = scheduler.main(
            ["--once", "--dry-run"],
            db=db,
            root=str(REPO),
            home=cli_world["home"],
        )

    assert rc == 0
    assert "next=e1" in output.getvalue()
    assert _status(db, "e1") == "not started"


def test_budget_cap_returns_two(cli_world: dict[str, str]) -> None:
    db = cli_world["db"]
    _seed(db, "e1", priority=5)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO task_runs "
        "(id,task_class,recipe,workflow_version,kickoff_path,status,cost_usd,created_at,updated_at) "
        "VALUES ('r1','x',NULL,'latest','k','classified',99.0,strftime('%s','now'),strftime('%s','now'))"
    )
    con.commit()
    con.close()

    result = subprocess.run(
        [str(BIN), "--once", "--budget-cap-usd", "10"],
        capture_output=True,
        text=True,
        env=_cli_env(cli_world),
    )

    assert result.returncode == 2
    assert "refusing dispatch" in result.stderr


def test_cost_pause_returns_two(cli_world: dict[str, str]) -> None:
    _seed(cli_world["db"], "e1")
    Path(cli_world["home"], "cost-pause.sentinel").touch()
    assert scheduler.main(
        ["--once"],
        db=cli_world["db"],
        root=str(REPO),
        home=cli_world["home"],
    ) == 2


def test_empty_queue_once_returns_zero(cli_world: dict[str, str]) -> None:
    _seed(cli_world["db"], "done1", status="done", priority=5)
    result = subprocess.run(
        [str(BIN), "--once"],
        capture_output=True,
        text=True,
        env=_cli_env(cli_world),
    )
    assert result.returncode == 0
    assert "queue empty" in result.stdout


def test_missing_db_returns_one(tmp_path: Path) -> None:
    missing = str(tmp_path / "nope.db")
    assert scheduler.main(
        ["--once"], db=missing, root=str(REPO), home=str(tmp_path)
    ) == 1


def test_help_and_invalid_flag_contract() -> None:
    help_result = subprocess.run([str(BIN), "--help"], capture_output=True, text=True)
    bad_result = subprocess.run([str(BIN), "bogus"], capture_output=True, text=True)
    missing_value = subprocess.run(
        [str(BIN), "--max-iters"], capture_output=True, text=True
    )

    assert help_result.returncode == 0
    assert "MO_DAILY_BUDGET_USD" in help_result.stdout
    assert "max-iters reached" in help_result.stdout
    assert bad_result.returncode == 2
    assert "unknown flag bogus" in bad_result.stderr
    assert missing_value.returncode == 2
    assert "requires a value" in missing_value.stderr
