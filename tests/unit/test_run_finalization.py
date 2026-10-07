"""K0.5a: no run ends without a recorded terminal status, and failure reasons reach a log.

AC1 the publisher's status write proves it landed; AC2 a run that passed but
has no publish step ends ``published``; AC3 execute stderr lands in execute.log.
(The execute.py:1133 half of AC1 is blocked on the revise-loop claim.)
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.cli import main as cli_main
from mini_ork.cli import publisher
from mini_ork.orchestration import run_reaper
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


def _seed(home: Path, run_id: str, status: str = "executing", verdict: str | None = None) -> Path:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, task_class, "
        "kickoff_path, workflow_version) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "verified-artifact", status, 0.0, now - 60, now, "verified_artifact", "", "latest"))
    con.commit()
    con.close()
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    if verdict:
        (run_dir / "verdict.json").write_text(json.dumps({"verdict": verdict, "failed_nodes": 0}), encoding="utf-8")
    return run_dir


def _row(home: Path, run_id: str) -> dict:
    con = sqlite3.connect(home / "state.db")
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute("SELECT status, verdict, notes, ended_at FROM task_runs WHERE id = ?",
                                (run_id,)).fetchone())
    finally:
        con.close()


# ── AC1 ─────────────────────────────────────────────────────────────────────

def test_publisher_status_write_raises_when_it_does_not_persist(home: Path, monkeypatch) -> None:
    _seed(home, "run-lost")
    # execute.set_status exhausting its retries looks exactly like this: no write, no error.
    monkeypatch.setattr("mini_ork.cli.execute.set_status", lambda db, run_id, new_status, **kw: None)
    with pytest.raises(RuntimeError, match="did not persist"):
        publisher.set_status(str(home / "state.db"), "run-lost", "published")


def test_publisher_status_write_that_lands_does_not_raise(home: Path) -> None:
    _seed(home, "run-ok")
    publisher.set_status(str(home / "state.db"), "run-ok", "published")
    assert _row(home, "run-ok")["status"] == "published"


def test_execute_set_status_raises_when_the_db_stays_locked(home: Path, monkeypatch) -> None:
    from mini_ork.cli import execute as ex

    _seed(home, "run-locked")
    real_connect = ex.sqlite3.connect

    class Locked:
        def __init__(self, *a, **kw): self._c = real_connect(*a, **kw)
        def execute(self, sql, *args):
            if sql.lstrip().upper().startswith("UPDATE"):
                raise ex.sqlite3.OperationalError("database is locked")
            return self._c.execute(sql, *args)
        def commit(self): self._c.commit()
        def close(self): self._c.close()

    monkeypatch.setattr(ex.sqlite3, "connect", Locked)
    monkeypatch.setattr(ex.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="could not be written: database is locked"):
        ex.set_status(str(home / "state.db"), "run-locked", "failed")


def test_execute_set_status_fails_fast_on_a_non_transient_error(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.cli import execute as ex

    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()  # no task_runs table
    slept: list[float] = []
    monkeypatch.setattr(ex.time, "sleep", slept.append)
    with pytest.raises(RuntimeError, match="no such table"):
        ex.set_status(str(db), "run-x", "executing")
    assert slept == []  # a missing table does not heal by waiting


def test_execute_set_status_still_writes_normally(home: Path) -> None:
    from mini_ork.cli import execute as ex

    _seed(home, "run-fine")
    ex.set_status(str(home / "state.db"), "run-fine", "failed")
    assert _row(home, "run-fine")["status"] == "failed"


# ── AC2 ─────────────────────────────────────────────────────────────────────

def test_lifecycle_teardown_publishes_a_passed_run_with_no_publish_step(home: Path) -> None:
    run_dir = _seed(home, "run-pass", verdict="pass")
    assert run_reaper.close_run_record(home / "state.db", "run-pass", run_dir, crashed=False, rc=0) == "executing"
    row = _row(home, "run-pass")
    assert row["status"] == "published" and row["ended_at"] is not None
    assert "no publish step" in row["notes"]


@pytest.mark.parametrize("rc, crashed, marker, note", [
    (1, False, None, "verify or a later step failed"),   # verify rejected after a passing execute
    (0, False, ".deadline-hit", "deadline hit"),          # deadline exit: verify never ran
    (None, True, None, "exception"),                      # crashed lifecycle
])
def test_a_passing_verdict_is_not_published_without_a_clean_finish(home, rc, crashed, marker, note) -> None:
    run_dir = _seed(home, "run-x", verdict="pass")
    if marker:
        (run_dir / marker).write_text("{}", encoding="utf-8")
    run_reaper.close_run_record(home / "state.db", "run-x", run_dir, crashed=crashed, rc=rc)
    row = _row(home, "run-x")
    assert row["status"] == "failed" and note in row["notes"]


def test_finalize_passed_never_overrides_a_terminal_status(home: Path) -> None:
    _seed(home, "run-done", status="failed")
    assert run_reaper.finalize_passed(home / "state.db", "run-done", "x") is False
    assert _row(home, "run-done")["status"] == "failed"


def test_publisher_without_an_artifact_contract_finalizes_the_run(home, tmp_path, monkeypatch, capsys) -> None:
    run_dir = _seed(home, "run-nocontract", verdict="pass")
    monkeypatch.setenv("MO_ORACLE_GATES_AUTO", "0")
    monkeypatch.setenv("MO_LEVEL_VECTOR", "0")
    monkeypatch.setenv("MINI_ORK_RECIPE_ROOT", str(tmp_path / "no-recipes-here"))
    rc, reason = publisher.publisher_node(str(REPO), str(run_dir), str(home / "state.db"), "run-nocontract",
                                          "verified-artifact", "verified_artifact")
    assert (rc, reason) == (0, "done")
    row = _row(home, "run-nocontract")
    assert row["status"] == "published" and "no artifact_contract.yaml" in row["notes"]
    assert "nothing delivered" in capsys.readouterr().out  # the reason reaches stdout (execute.log) too


# ── AC3 ─────────────────────────────────────────────────────────────────────

def test_execute_log_keeps_stderr(tmp_path: Path) -> None:
    cli_main._write_execute_log(str(tmp_path), "  [ok] node a\n", "  [fail] publisher: 1 of 1 outputs failed\n")
    log = (tmp_path / cli_main._execute_log_name()).read_text(encoding="utf-8")
    assert log.index("[ok] node a") < log.index("── execute stderr ──") < log.index("[fail] publisher")


def test_execute_log_without_stderr_has_no_marker(tmp_path: Path) -> None:
    cli_main._write_execute_log(str(tmp_path), "  [ok] node a\n", "")
    assert "execute stderr" not in (tmp_path / cli_main._execute_log_name()).read_text(encoding="utf-8")
