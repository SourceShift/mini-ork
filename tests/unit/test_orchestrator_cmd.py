"""Unit tests for the detached ``mini-ork orchestrator watch`` shell.

The policy is tested in ``test_runner_orchestrator_watch.py``. This file pins
the process contract: argv/home validation, the single-owner lock, and the one
``--once`` tick against a seeded run dir and state.db.
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time

import pytest

from mini_ork.cli import orchestrator_cmd as cmd
from mini_ork.orchestration import runner_orchestrator as orch


def _make_db(home, *, status="executing", run_id="run-1"):
    db = home / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT, created_at INTEGER);
        CREATE TABLE run_events (
            event_id TEXT PRIMARY KEY, run_id TEXT, event_type TEXT,
            payload_json TEXT, created_at INTEGER
        );
        CREATE TABLE operator_steering (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, role_target TEXT,
            severity TEXT, message TEXT, source TEXT, confidence REAL,
            created_at INTEGER, expires_at INTEGER
        );
        """
    )
    con.execute("INSERT INTO task_runs(id, status, created_at) VALUES (?, ?, ?)",
                (run_id, status, int(time.time()) - 1000))
    con.commit()
    con.close()
    return db


def _seed_run(tmp_path, *, run_id="run-1", status="executing", log_lines=None):
    home = tmp_path / "home"
    home.mkdir()
    _make_db(home, status=status, run_id=run_id)
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / ".pid").write_text(str(os.getpid()), encoding="utf-8")
    started = int(time.time()) - 900
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("evt-1", run_id, "node_start",
         json.dumps({"node_id": "impl", "node_type": "implementer"}), started),
    )
    con.commit()
    con.close()
    log = run_dir / "verifier_impl.log"
    log.write_text("\n".join(log_lines or ["boom: the same failure"] * 4) + "\n",
                   encoding="utf-8")
    old = time.time() - 900
    os.utime(log, (old, old))
    return home, run_dir


def _events(home):
    con = sqlite3.connect(home / "state.db")
    try:
        return con.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id = 'run-1' "
            "ORDER BY rowid").fetchall()
    finally:
        con.close()


def _steers(home):
    con = sqlite3.connect(home / "state.db")
    try:
        return con.execute("SELECT message, source FROM operator_steering").fetchall()
    finally:
        con.close()


def test_explicit_uninitialised_home_exits_3(tmp_path, capsys):
    assert cmd.main(["watch", "run-1", "--home", str(tmp_path)]) == 3
    assert "not an initialised" in capsys.readouterr().err


def test_unsafe_run_id_exits_2(tmp_path, capsys):
    assert cmd.main(["watch", "../etc", "--home", str(tmp_path)]) == 2
    assert "invalid run id" in capsys.readouterr().err


def test_missing_run_dir_exits_2(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    _make_db(home)
    assert cmd.main(["watch", "run-9", "--home", str(home)]) == 2
    assert "no such run dir" in capsys.readouterr().err


def test_default_home_simple_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "missing-but-unvalidated"))
    assert cmd._resolve_home(None) == tmp_path / "missing-but-unvalidated"


def test_explicit_home_requires_state_or_config(tmp_path):
    (tmp_path / "config").mkdir()
    assert cmd._resolve_home(str(tmp_path)) == tmp_path.absolute()


def test_second_watcher_is_a_noop(tmp_path, capsys):
    home, run_dir = _seed_run(tmp_path)
    handle = open(run_dir / orch.WATCH_LOCK_FILE, "a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert cmd.main(["watch", "run-1", "--home", str(home)]) == 0
        assert "already watching" in capsys.readouterr().out
    finally:
        handle.close()


def test_once_emits_watching_signal_and_steers(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_ORCH_WATCH_AUTONOMY", "steer")
    home, run_dir = _seed_run(tmp_path)
    pid_before = (run_dir / ".pid").read_text(encoding="utf-8")

    assert cmd.main(["watch", "run-1", "--home", str(home), "--once"]) == 0

    types = [event_type for event_type, _ in _events(home)]
    ours = [t for t in types if t.startswith("orchestrator.")]
    assert ours[0] == "orchestrator.watching"
    assert "orchestrator.signal" in ours
    assert all(not t.startswith("monitor.") for t in types)
    assert "orchestrator.ended" not in ours

    payloads = [json.loads(p) for t, p in _events(home) if t == "orchestrator.signal"]
    assert any(p.get("kind") == "stalled" and p.get("node_id") == "impl"
               for p in payloads)

    steers = _steers(home)
    assert len(steers) == 1
    assert steers[0][1] == "orchestrator-watch"
    assert "Orchestrator note for impl" in steers[0][0]
    assert (run_dir / ".pid").read_text(encoding="utf-8") == pid_before
    # State is persisted for the next reader; a one-shot tick is not an end.
    state = orch.load_watch_state(run_dir)
    assert state.tick_count == 1
    assert state.ended_reason is None


def test_once_notify_writes_no_steering(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_ORCH_WATCH_AUTONOMY", "notify")
    home, _ = _seed_run(tmp_path)
    assert cmd.main(["watch", "run-1", "--home", str(home), "--once"]) == 0
    assert _steers(home) == []


def test_corrupt_state_file_recovers(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_ORCH_WATCH_AUTONOMY", "notify")
    home, run_dir = _seed_run(tmp_path)
    orch.watch_state_path(run_dir).write_text("{", encoding="utf-8")
    assert cmd.main(["watch", "run-1", "--home", str(home), "--once"]) == 0
    assert orch.load_watch_state(run_dir).tick_count == 1


def test_terminal_run_ends_instead_of_sampling(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_ORCH_WATCH_AUTONOMY", "notify")
    home, _ = _seed_run(tmp_path, status="published")
    assert cmd.main(["watch", "run-1", "--home", str(home), "--once"]) == 0
    types = [event_type for event_type, _ in _events(home)]
    assert types[-1] == "orchestrator.ended"
    payload = json.loads(_events(home)[-1][1])
    assert payload["exit"] == "terminal:published"


def test_missing_db_still_watches(tmp_path, monkeypatch):
    """The simple tier starts before state.db exists; reads degrade, no crash."""
    monkeypatch.setenv("MO_ORCH_WATCH_AUTONOMY", "notify")
    home = tmp_path / "home"
    run_dir = home / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / ".pid").write_text(str(os.getpid()), encoding="utf-8")
    (home / "config").mkdir()
    assert cmd.main(["watch", "run-1", "--home", str(home), "--once"]) == 0
    assert orch.load_watch_state(run_dir).tick_count == 1
    # No database to write to: the decision was taken and could not be stored.
    assert not (home / "state.db").exists()


def test_flag_off_ends_the_watch(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_ORCH_WATCH", "0")
    home, _ = _seed_run(tmp_path)
    assert cmd.main(["watch", "run-1", "--home", str(home), "--once"]) == 0
    payload = json.loads(_events(home)[-1][1])
    assert payload["exit"] == "disabled"


def test_redacted_detail_still_steers(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_ORCH_WATCH_AUTONOMY", "steer")
    home, _ = _seed_run(tmp_path, log_lines=["schema mismatch on exit_code"] * 4)
    assert cmd.main(["watch", "run-1", "--home", str(home), "--once"]) == 0
    steers = _steers(home)
    assert len(steers) == 1
    assert "schema" not in steers[0][0]
    assert "exit_code" not in steers[0][0]


@pytest.mark.parametrize("verb", [None, "nope"])
def test_usage_without_a_known_verb(capsys, monkeypatch, verb):
    argv = [] if verb is None else [verb]
    assert cmd.main(argv) == 2
    assert "usage:" in capsys.readouterr().err
