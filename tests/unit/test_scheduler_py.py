"""Standalone contracts for the canonical native epic scheduler."""
from __future__ import annotations

import io
import json
import os
import socket
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


def _notes(db: str, epic_id: str) -> str:
    con = sqlite3.connect(db)
    row = con.execute("SELECT notes FROM epics WHERE id=?", (epic_id,)).fetchone()
    con.close()
    return str(row[0]) if row and row[0] else ""


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
    """An extensionless launcher is compiled as Python, by the interpreter
    running THIS code.

    It used to say ``python3``, which on this host is 3.9 and cannot import
    ``mini_ork`` at all — so the generated check failed for every epic that had
    one, on the interpreter alone.
    """
    commands = epics._synth_verify(["bin/mini-ork-scheduler"])
    assert commands == [f"{sys.executable} -m py_compile bin/mini-ork-scheduler"]


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
    """The cap is enforced against the per-dispatch ledger (llm_calls), the
    meter that sees every provider call — node or stage."""
    db = cli_world["db"]
    _seed(db, "e1", priority=5)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO llm_calls "
        "(provider,model_id,tier,feature_name,cost_usd,status,ts) "
        "VALUES ('deepseek','deepseek-v4-flash','default','mini-ork:gradient-extract',"
        "99.0,'success',strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
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


# ── the daily budget meter ──────────────────────────────────────────────────
# today_cost_usd() is what MO_DAILY_BUDGET_USD is enforced against. It summed
# task_runs.cost_usd, which only carries what a node handler explicitly
# charged — stage spend (reflect/gradient-extract, the jury, the lens panel)
# never reaches a charge call. Measured 2026-09-25: $3.1302 of real dispatch
# spend in 24h read as $0.00, so the circuit could not fire.

def _insert_llm_call(db: str, cost: float, ts: str) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO llm_calls (provider,model_id,tier,feature_name,cost_usd,status,ts) "
        "VALUES ('deepseek','deepseek-v4-flash','default','mini-ork:gradient-extract',?,"
        "'success',?)",
        (cost, ts),
    )
    con.commit()
    con.close()


def _insert_task_run(db: str, cost: float, created_at: int) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO task_runs (id,task_class,kickoff_path,cost_usd,created_at,updated_at) "
        "VALUES (?,'code-fix','k.md',?,?,?)",
        (f"tr-{created_at}-{cost}", cost, created_at, created_at),
    )
    con.commit()
    con.close()


def test_today_cost_usd_sums_llm_calls(tmp_path: Path) -> None:
    """The meter reads the per-dispatch ledger every provider call writes.
    Timestamps are now-offset, never absolute — a rolling-window test seeded
    with a literal date reds out once that date ages past 24h."""
    db = _init_db(tmp_path / "meter")
    now = time.time()

    def fmt(secs: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))

    _insert_llm_call(db, 0.177876, fmt(now - 600))
    _insert_llm_call(db, 0.174592, fmt(now - 3600))
    assert scheduler.today_cost_usd(db) == pytest.approx(0.352468)

    # A row older than the rolling 24h window is excluded.
    _insert_llm_call(db, 99.0, fmt(now - 30 * 3600))
    assert scheduler.today_cost_usd(db) == pytest.approx(0.352468)


def test_today_cost_usd_falls_back_to_task_runs(tmp_path: Path) -> None:
    """A DB behind the migrator (no llm_calls) degrades to the old estimate
    rather than to a blind zero."""
    db = _init_db(tmp_path / "meter-fallback")
    now = int(time.time())
    _insert_task_run(db, 1.25, now - 3600)
    con = sqlite3.connect(db)
    con.execute("DROP TABLE llm_calls")
    con.commit()
    con.close()

    assert scheduler.today_cost_usd(db) == pytest.approx(1.25)


# ── stale 'in progress' reclaim ──────────────────────────────────────────────

def test_reclaim_returns_a_wedged_epic_to_the_queue(tmp_path: Path) -> None:
    """``pick_ready`` only selects 'not started', so an epic left 'in progress'
    by a killed scheduler is unpickable forever — the queue drains to nothing
    while real work remains. Startup is the right moment to repair it."""
    db = _init_db(tmp_path / "reclaim")
    _seed(db, "epic-wedged", status="in progress")

    assert scheduler.reclaim_stale(db, older_than_s=0) == ["epic-wedged"]
    assert _status(db, "epic-wedged") == "not started"


def test_reclaim_leaves_a_live_run_alone(tmp_path: Path) -> None:
    """A second scheduler sharing the database must not steal work mid-flight."""
    db = _init_db(tmp_path / "reclaim-live")
    scheduler.ensure_retry_schema(db)  # adds last_run_id; 0001 does not
    _seed(db, "epic-running", status="in progress")
    con = sqlite3.connect(db)
    con.execute("UPDATE epics SET last_run_id='run-live' WHERE id='epic-running'")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, task_class, kickoff_path, created_at, updated_at) "
        "VALUES ('run-live', 'epic-runner', 'executing', 'epic', '', 0, 0)")
    con.commit()
    con.close()

    assert scheduler.reclaim_stale(db, older_than_s=0) == []
    assert _status(db, "epic-running") == "in progress"


def test_reclaim_ignores_a_run_that_already_ended(tmp_path: Path) -> None:
    """The epic is only wedged when the run it points at is gone."""
    db = _init_db(tmp_path / "reclaim-dead")
    scheduler.ensure_retry_schema(db)  # adds last_run_id; 0001 does not
    _seed(db, "epic-abandoned", status="in progress")
    con = sqlite3.connect(db)
    con.execute("UPDATE epics SET last_run_id='run-dead' WHERE id='epic-abandoned'")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, task_class, kickoff_path, created_at, updated_at, ended_at) "
        "VALUES ('run-dead', 'epic-runner', 'failed', 'epic', '', 0, 0, 1)")
    con.commit()
    con.close()

    assert scheduler.reclaim_stale(db, older_than_s=0) == ["epic-abandoned"]
    assert "reclaimed from stale 'in progress'" in _notes(db, "epic-abandoned")


def test_the_grace_window_protects_a_freshly_touched_epic(tmp_path: Path) -> None:
    db = _init_db(tmp_path / "reclaim-grace")
    _seed(db, "epic-fresh", status="in progress")

    assert scheduler.reclaim_stale(db, older_than_s=900) == []
    assert _status(db, "epic-fresh") == "in progress"


def _live_epic_run(db: str, run_id: str, kickoff_path: str, *, ended: bool = False) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, task_class, kickoff_path, created_at, updated_at, ended_at) "
        "VALUES (?, 'epic-runner', ?, 'epic', ?, 0, 0, ?)",
        (run_id, "failed" if ended else "executing", kickoff_path, 1 if ended else None))
    con.commit()
    con.close()


def test_a_first_attempt_in_flight_is_not_reclaimed(tmp_path: Path) -> None:
    """last_run_id is written when an attempt finishes, so a first attempt in
    flight has none. Its live run is found by kickoff — under any engine root."""
    db = _init_db(tmp_path / "reclaim-first")
    scheduler.ensure_retry_schema(db)
    _seed(db, "epic-first", status="in progress")
    con = sqlite3.connect(db)
    con.execute("UPDATE epics SET kickoff_path='kickoffs/auto/epic-first.md' WHERE id='epic-first'")
    con.commit()
    con.close()
    _live_epic_run(db, "run-a", "/ps/mini-ork-worktrees/engine-tip/kickoffs/auto/epic-first.md")

    assert scheduler.reclaim_stale(db, older_than_s=0) == []
    assert _status(db, "epic-first") == "in progress"


def test_a_retry_in_flight_is_found_by_its_carry_over_kickoff(tmp_path: Path) -> None:
    db = _init_db(tmp_path / "reclaim-retry")
    scheduler.ensure_retry_schema(db)
    _seed(db, "epic-retry", status="in progress")
    _live_epic_run(db, "run-b", "/h/.mini-ork/runs/scheduler/kickoffs/epic-retry-attempt-2.md")

    assert scheduler.reclaim_stale(db, older_than_s=0) == []


def test_an_ended_run_on_the_same_kickoff_does_not_protect_the_epic(tmp_path: Path) -> None:
    db = _init_db(tmp_path / "reclaim-ended")
    scheduler.ensure_retry_schema(db)
    _seed(db, "epic-dead", status="in progress")
    con = sqlite3.connect(db)
    con.execute("UPDATE epics SET kickoff_path='kickoffs/auto/epic-dead.md' WHERE id='epic-dead'")
    con.commit()
    con.close()
    _live_epic_run(db, "run-c", "/ps/mini-ork/kickoffs/auto/epic-dead.md", ended=True)

    assert scheduler.reclaim_stale(db, older_than_s=0) == ["epic-dead"]


def _claimed_by(db: str, epic_id: str) -> str | None:
    con = sqlite3.connect(db)
    try:
        row = con.execute("SELECT claimed_by FROM epics WHERE id=?", (epic_id,)).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def _set_claimer(db: str, epic_id: str, claimed_by: str) -> None:
    con = sqlite3.connect(db)
    con.execute("UPDATE epics SET claimed_by=? WHERE id=?", (claimed_by, epic_id))
    con.commit()
    con.close()


def test_a_claim_records_the_claiming_scheduler(tmp_path: Path) -> None:
    db = _init_db(tmp_path / "claim-who")
    _seed(db, "epic-c")

    assert scheduler._claim(db, "epic-c") is True
    assert _claimed_by(db, "epic-c") == f"{socket.gethostname()}:{os.getpid()}"


def test_a_live_claimer_protects_an_epic_whose_run_already_ended(tmp_path: Path, monkeypatch) -> None:
    """The settle window: the run is over, its scheduler is still running hooks.
    Neither last_run_id nor a live task_runs row exists to protect the epic."""
    db = _init_db(tmp_path / "claim-live")
    scheduler.ensure_retry_schema(db)
    _seed(db, "epic-settling", status="in progress")
    _set_claimer(db, "epic-settling", f"{socket.gethostname()}:4242")
    _live_epic_run(db, "run-done", "/ps/mini-ork/kickoffs/auto/epic-settling.md", ended=True)
    monkeypatch.setattr(scheduler, "_is_scheduler_process", lambda pid: pid == 4242)

    assert scheduler.reclaim_stale(db, older_than_s=0) == []
    assert _status(db, "epic-settling") == "in progress"


def test_a_dead_claimer_or_another_hosts_claim_does_not_protect(tmp_path: Path, monkeypatch) -> None:
    db = _init_db(tmp_path / "claim-dead")
    scheduler.ensure_retry_schema(db)
    _seed(db, "epic-dead-claimer", status="in progress")
    _seed(db, "epic-other-host", status="in progress")
    _set_claimer(db, "epic-dead-claimer", f"{socket.gethostname()}:4243")
    _set_claimer(db, "epic-other-host", "some-other-box:4242")
    monkeypatch.setattr(scheduler, "_is_scheduler_process", lambda pid: pid == 4242)

    assert sorted(scheduler.reclaim_stale(db, older_than_s=0)) == ["epic-dead-claimer", "epic-other-host"]


def test_a_reused_pid_that_is_not_a_scheduler_does_not_protect(tmp_path: Path) -> None:
    """This test process is alive but is not a scheduler — its pid must not count."""
    db = _init_db(tmp_path / "claim-reused")
    scheduler.ensure_retry_schema(db)
    _seed(db, "epic-reused", status="in progress")
    _set_claimer(db, "epic-reused", f"{socket.gethostname()}:{os.getpid()}")

    assert scheduler.reclaim_stale(db, older_than_s=0) == ["epic-reused"]


def test_reclaim_is_a_no_op_on_an_empty_queue(tmp_path: Path) -> None:
    assert scheduler.reclaim_stale(_init_db(tmp_path / "reclaim-empty")) == []


# ── _verdict_from_log: two verdict shapes, one reader ────────────────────────
def _verdict_home(tmp_path: Path, run_id: str, **files: object) -> tuple[str, str]:
    """A home whose runs/<run_id>/ holds the given verdict files, plus the
    dispatcher log naming that run. ``files`` keys are file stems."""
    home = tmp_path / "vh"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    for name, payload in files.items():
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (run_dir / f"{name}.json").write_text(text, encoding="utf-8")
    log = tmp_path / "dispatch.log"
    log.write_text(f"some runner output\nrun_id={run_id}\n", encoding="utf-8")
    return str(log), str(home)


def test_framework_edit_reads_its_run_verdict_word(tmp_path: Path) -> None:
    """framework-edit's own verdict.json carries ``pass`` and not ``verdict``;
    the word lives in run-verdict.json — reading only the first two files made
    every such run 'unknown' and escalated it however well it ran."""
    log, home = _verdict_home(
        tmp_path, "run-fe",
        **{"verdict": {"files_changed": 3, "tests_pass": True, "static_pass": True, "pass": True},
           "run-verdict": {"verdict": "pass", "failed_nodes": 0},
           "panel-verdict": {"panel_score": 87.5, "pass": True, "source": "rubric-prescreen"}})
    assert scheduler._verdict_from_log(log, home) == "pass"


def test_a_bare_boolean_is_read_when_no_file_carries_a_word(tmp_path: Path) -> None:
    """The fallback: a run whose only verdicts are booleans still resolves."""
    log, home = _verdict_home(tmp_path, "run-bool", **{"verdict": {"pass": True}})
    assert scheduler._verdict_from_log(log, home) == "pass"
    log, home = _verdict_home(tmp_path, "run-bool2", **{"verdict": {"pass": False}})
    assert scheduler._verdict_from_log(log, home) == "fail"


def test_the_runs_own_final_word_outranks_the_rubric_prescreen(tmp_path: Path) -> None:
    """panel-verdict.json is an advisory prescreen, so it must not veto the run
    in the boolean fallback — but its *word* keeps its original precedence."""
    log, home = _verdict_home(
        tmp_path, "run-pre",
        **{"run-verdict": {"pass": False}, "panel-verdict": {"pass": True}})
    assert scheduler._verdict_from_log(log, home) == "fail"
    log, home = _verdict_home(
        tmp_path, "run-word",
        **{"run-verdict": {"verdict": "pass"}, "panel-verdict": {"verdict": "fail"}})
    assert scheduler._verdict_from_log(log, home) == "fail"


def test_no_verdict_anywhere_is_still_unknown(tmp_path: Path) -> None:
    log, home = _verdict_home(tmp_path, "run-empty", **{"verdict": {"files_changed": 1}})
    assert scheduler._verdict_from_log(log, home) == "unknown"
    log, home = _verdict_home(tmp_path, "run-junk", **{"verdict": "[1, 2, 3]"})
    assert scheduler._verdict_from_log(log, home) == "unknown"


def test_a_log_without_a_run_id_is_unknown(tmp_path: Path) -> None:
    log = tmp_path / "norun.log"
    log.write_text("runner said nothing useful\n", encoding="utf-8")
    assert scheduler._verdict_from_log(str(log), str(tmp_path)) == "unknown"
    assert scheduler._verdict_from_log(str(tmp_path / "absent.log"), str(tmp_path)) == "unknown"
