"""Run liveness + the orphaned-run reaper (``mini_ork/orchestration/run_reaper.py``).

A dispatcher that dies without its ``finally`` leaves ``task_runs.status``
non-terminal forever; the IDE's Active dispatches table then lists it as in
flight. These tests pin who may be reaped (a ``.pid`` whose process is gone)
and who must never be (a live owner, a paused or remote run, a row with no
owner record unless the operator opts in).
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

from mini_ork.cli import board_cmd
from mini_ork.cli import main as cli_main
from mini_ork.orchestration import run_reaper
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
HOUR = 3600


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed(home: Path, run_id: str, status: str = "executing", *, updated_at: int | None = None) -> Path:
    now = int(time.time()) if updated_at is None else updated_at
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, task_class, "
        "kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "code-fix", status, 0.0, now - 60, now, "code_fix", "", "latest"))
    con.commit()
    con.close()
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    return run_dir


def _row(home: Path, run_id: str) -> dict:
    con = sqlite3.connect(home / "state.db")
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute(
            "SELECT status, verdict, notes, ended_at FROM task_runs WHERE id = ?", (run_id,)).fetchone())
    finally:
        con.close()


def _event(home: Path, run_id: str, event_type: str, node_id: str, at: int) -> None:
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at) VALUES (?,?,?,?,?)",
        (f"evt-{run_id}-{event_type}-{node_id}-{at}", run_id, event_type, json.dumps({"node_id": node_id}), at))
    con.commit()
    con.close()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _write_pid(run_dir: Path, pid: int) -> Path:
    path = run_dir / ".pid"
    path.write_text(f"{pid}\n", encoding="utf-8")
    return path


# ── who is reaped ───────────────────────────────────────────────────────────

def test_a_pid_file_whose_process_is_gone_is_reaped_as_a_crash(home: Path) -> None:
    run_dir = _seed(home, "run-dead")
    _write_pid(run_dir, _dead_pid())
    _event(home, "run-dead", "node_start", "implementer", int(time.time()) - 30)

    reaped = run_reaper.reap(home)

    assert [r["run_id"] for r in reaped] == ["run-dead"]
    row = _row(home, "run-dead")
    assert row["status"] == "failed" and row["verdict"] == "CRASH"
    assert "reaped: dispatcher pid" in row["notes"]
    assert not (run_dir / ".pid").exists()
    # The dangling node_start is closed, so no DAG view shows it running.
    con = sqlite3.connect(home / "state.db")
    ends = con.execute("SELECT COUNT(*) FROM run_events WHERE run_id = 'run-dead' "
                       "AND event_type = 'node_end'").fetchone()[0]
    con.close()
    assert ends == 1


def test_runs_sharing_a_node_id_are_closed_in_one_pass(home: Path) -> None:
    # Regression: the synthetic node_end key had no run id, so the second run
    # with a dangling "implementer" in the same second hit UNIQUE(event_id)
    # and aborted the pass (researcher backfill, 2026-10-07).
    started = int(time.time()) - 30
    for run_id in ("run-a", "run-b", "run-c"):
        _write_pid(_seed(home, run_id), _dead_pid())
        _event(home, run_id, "node_start", "implementer", started)

    assert sorted(r["run_id"] for r in run_reaper.reap(home)) == ["run-a", "run-b", "run-c"]
    con = sqlite3.connect(home / "state.db")
    closed = dict(con.execute("SELECT run_id, COUNT(*) FROM run_events WHERE event_type = 'node_end' "
                              "GROUP BY run_id").fetchall())
    con.close()
    assert closed == {"run-a": 1, "run-b": 1, "run-c": 1}


def test_a_live_owner_is_left_alone(home: Path) -> None:
    run_dir = _seed(home, "run-live")
    _write_pid(run_dir, os.getpid())

    assert run_reaper.reap(home) == []
    assert _row(home, "run-live")["status"] == "executing"
    assert (run_dir / ".pid").exists()


def test_a_recycled_pid_does_not_keep_a_dead_run_alive(home: Path) -> None:
    # Falsifies the start-time guard: this process is alive, but it started
    # long after the .pid was written, so it cannot be the run's owner.
    run_dir = _seed(home, "run-recycled")
    pid_path = _write_pid(run_dir, os.getpid())
    long_ago = time.time() - 30 * 86400
    os.utime(pid_path, (long_ago, long_ago))

    assert run_reaper.probe(run_dir).verdict == "dead"
    assert [r["run_id"] for r in run_reaper.reap(home)] == ["run-recycled"]


def test_no_pid_file_is_unknown_and_kept_unless_the_operator_opts_in(home: Path) -> None:
    now = int(time.time())
    _seed(home, "run-idle", "classified", updated_at=now - 7 * HOUR)
    _seed(home, "run-fresh", "classified", updated_at=now - 60)
    # Old row, recent event: the event is a sign of life.
    _seed(home, "run-busy", "executing", updated_at=now - 7 * HOUR)
    _event(home, "run-busy", "node_start", "plan", now - 120)

    assert run_reaper.reap(home) == []

    reaped = run_reaper.reap(home, stale_after=6 * HOUR)
    assert [r["run_id"] for r in reaped] == ["run-idle"]
    row = _row(home, "run-idle")
    assert row["status"] == "failed" and row["verdict"] is None
    assert row["ended_at"] == now - 7 * HOUR  # ends at its last sign of life, not at reap time
    assert _row(home, "run-fresh")["status"] == "classified"
    assert _row(home, "run-busy")["status"] == "executing"


@pytest.mark.parametrize("marker", [".cost-pause", ".remote-sync-state.json"])
def test_paused_and_remote_runs_are_never_judged_by_a_local_pid(home: Path, marker: str) -> None:
    run_dir = _seed(home, "run-held")
    _write_pid(run_dir, _dead_pid())
    (run_dir / marker).write_text("{}", encoding="utf-8")

    assert run_reaper.reap(home) == []
    assert _row(home, "run-held")["status"] == "executing"


def test_a_run_whose_verdict_passed_is_never_labelled_failed(home: Path) -> None:
    # Regression: execute-only callers end a passing run with no publish step;
    # the stale pass of the 2026-10-07 researcher backfill marked 25 of them
    # failed. Neither a dead .pid nor --stale-after may fail finished work.
    old = int(time.time()) - 30 * HOUR
    dead_dir = _seed(home, "run-pass-dead")
    _write_pid(dead_dir, _dead_pid())
    stale_dir = _seed(home, "run-pass-stale", updated_at=old)
    for d in (dead_dir, stale_dir):
        (d / "verdict.json").write_text(json.dumps({"verdict": "pass", "failed_nodes": 0}), encoding="utf-8")
    fail_dir = _seed(home, "run-fail-stale", updated_at=old)
    (fail_dir / "verdict.json").write_text(json.dumps({"verdict": "fail", "failed_nodes": 2}), encoding="utf-8")

    assert run_reaper.probe(dead_dir).verdict == "finished"
    assert [r["run_id"] for r in run_reaper.reap(home, stale_after=6 * HOUR)] == ["run-fail-stale"]
    assert _row(home, "run-pass-dead")["status"] == "executing"
    assert _row(home, "run-pass-stale")["status"] == "executing"
    assert run_reaper.close_run_record(home / "state.db", "run-pass-dead", dead_dir, crashed=False) is None


def test_terminal_rows_are_not_candidates(home: Path) -> None:
    run_dir = _seed(home, "run-done", "published")
    _write_pid(run_dir, _dead_pid())

    assert run_reaper.reap(home) == []
    row = _row(home, "run-done")
    assert row["status"] == "published" and row["verdict"] is None
    assert (run_dir / ".pid").exists()


def test_dry_run_writes_nothing(home: Path) -> None:
    run_dir = _seed(home, "run-dead")
    _write_pid(run_dir, _dead_pid())

    assert [r["run_id"] for r in run_reaper.reap(home, dry_run=True)] == ["run-dead"]
    assert _row(home, "run-dead")["status"] == "executing"
    assert (run_dir / ".pid").exists()


def test_a_row_that_moved_on_after_the_read_is_not_overwritten(home: Path, monkeypatch) -> None:
    run_dir = _seed(home, "run-raced")
    _write_pid(run_dir, _dead_pid())
    real_probe = run_reaper.probe

    def probe_then_publish(path: Path) -> run_reaper.Probe:
        found = real_probe(path)
        con = sqlite3.connect(home / "state.db")
        con.execute("UPDATE task_runs SET status = 'published', updated_at = updated_at + 1 "
                    "WHERE id = 'run-raced'")
        con.commit()
        con.close()
        return found

    monkeypatch.setattr(run_reaper, "probe", probe_then_publish)
    assert run_reaper.reap(home) == []
    assert _row(home, "run-raced")["status"] == "published"


# ── the owner record the lifecycle writes ───────────────────────────────────

def test_release_drops_only_a_pid_file_naming_this_process(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r1"
    run_reaper.claim_pid_file(run_dir)
    assert (run_dir / ".pid").read_text().split() == [str(os.getpid())]
    run_reaper.release_pid_file(run_dir)
    assert not (run_dir / ".pid").exists()

    _write_pid(run_dir, os.getpid() + 1)  # someone else's claim
    run_reaper.release_pid_file(run_dir)
    assert (run_dir / ".pid").exists()


def test_close_run_record_ends_a_non_terminal_run(home: Path) -> None:
    run_dir = _seed(home, "run-aborted")
    assert run_reaper.close_run_record(home / "state.db", "run-aborted", run_dir, crashed=False) == "executing"
    row = _row(home, "run-aborted")
    assert row["status"] == "failed" and row["verdict"] is None
    assert "without a verdict" in row["notes"]

    crashed_dir = _seed(home, "run-crashed", "planned")
    run_reaper.close_run_record(home / "state.db", "run-crashed", crashed_dir, crashed=True)
    assert _row(home, "run-crashed")["verdict"] == "CRASH"

    deadline_dir = _seed(home, "run-deadline")
    (deadline_dir / ".deadline-hit").write_text("{}", encoding="utf-8")
    run_reaper.close_run_record(home / "state.db", "run-deadline", deadline_dir, crashed=False)
    assert "deadline hit" in _row(home, "run-deadline")["notes"]


def test_close_run_record_leaves_terminal_and_paused_runs(home: Path) -> None:
    done_dir = _seed(home, "run-done", "published")
    assert run_reaper.close_run_record(home / "state.db", "run-done", done_dir, crashed=True) is None
    assert _row(home, "run-done")["status"] == "published"

    paused_dir = _seed(home, "run-paused")
    (paused_dir / ".cost-pause").write_text("", encoding="utf-8")
    assert run_reaper.close_run_record(home / "state.db", "run-paused", paused_dir, crashed=False) is None
    assert _row(home, "run-paused")["status"] == "executing"


def test_lifecycle_teardown_writes_status_then_releases_the_pid(home: Path, monkeypatch) -> None:
    run_dir = _seed(home, "run-teardown")
    run_reaper.claim_pid_file(run_dir)
    monkeypatch.setenv("MINI_ORK_DB", str(home / "state.db"))
    sink = {"run_id": "run-teardown", "_pid_run_dir": str(run_dir)}

    cli_main._close_run_record(sink, crashed=False)

    assert "_pid_run_dir" not in sink  # never leaks into the --json result
    assert _row(home, "run-teardown")["status"] == "failed"
    assert not (run_dir / ".pid").exists()


# ── surfaces ────────────────────────────────────────────────────────────────

def test_the_board_read_reaps_before_it_lists(home: Path, capsys) -> None:
    run_dir = _seed(home, "run-dead")
    _write_pid(run_dir, _dead_pid())

    assert board_cmd.main(["--home", str(home), "--json", "--shell"], "") == 0
    payload = json.loads(capsys.readouterr().out)

    assert _row(home, "run-dead")["status"] == "failed"
    assert "reaper" not in payload.get("errors", {})
    assert {r["id"]: r["state"] for r in payload["runs"]}["run-dead"] == "failed"


def test_cli_json_and_usage(home: Path, capsys) -> None:
    run_dir = _seed(home, "run-dead")
    _write_pid(run_dir, _dead_pid())

    assert run_reaper.main(["--home", str(home), "--json", "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["dry_run"] and [r["run_id"] for r in out["reaped"]] == ["run-dead"]

    assert run_reaper.main(["--home", str(home / "nope")]) == 2
    with pytest.raises(SystemExit):
        run_reaper.main(["--home", str(home), "--stale-after", "6 hours"])
