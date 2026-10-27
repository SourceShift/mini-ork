"""Hermetic tests for the live ``MINI_ORK_ON_EVENT`` wiring.

A tmp executable script appends ``"$1 $2 $3"`` to a file; a real state DB is
created with :func:`mini_ork.stores.migrate.init_db`. Each test drives one of
the emit points and waits (bounded) for the detached hook process to write.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

import mini_ork
from mini_ork.stores.migrate import init_db

REPO = Path(mini_ork.__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("event-hook-db")
    p = d / "state.db"
    rc, out, err = init_db(db=str(p), root=str(REPO))
    assert rc == 0, err or out
    return p


class _FakeProc:
    """Stands in for Popen; usable by subprocess.run too."""

    pid = os.getpid()
    returncode = 0
    args: tuple[str, ...] = ()

    def wait(self, *a, **k):
        return 0

    def poll(self, *a, **k):
        return 0

    def kill(self):
        return None

    def communicate(self, *a, **k):
        return ("", "")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _hook_script(tmp_path: Path, name: str = "hook.sh") -> tuple[Path, Path]:
    script = tmp_path / name
    script.write_text(
        '#!/bin/sh\necho "$1 $2 $3" >> "$(dirname "$0")/events.log"\n',
        encoding="utf-8")
    script.chmod(0o755)
    return script, tmp_path / "events.log"


def _wait_for(predicate, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _lines(log: Path) -> list[str]:
    if not log.is_file():
        return []
    return [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("MINI_ORK_DB", str(db_path))
    monkeypatch.delenv("MINI_ORK_ON_EVENT", raising=False)
    monkeypatch.delenv("MINI_ORK_ON_EVENT_HEARTBEATS", raising=False)
    from mini_ork.observability import emit_hook as mod
    monkeypatch.setattr(mod, "_WARNED_INVALID", False)


# ── 1. node_start / node_end ───────────────────────────────────────────────


def test_node_start_and_end_fire_hook(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.observability.node_events import mo_node_end, mo_node_start
    script, log = _hook_script(tmp_path)
    monkeypatch.setenv("MINI_ORK_ON_EVENT", str(script))
    run = "run-wired-node"
    mo_node_start(run, "n1", "implementer")
    mo_node_end(run, "n1", "implementer", 42, verdict="APPROVE")

    assert _wait_for(lambda: {"node_start", "node_end"} <=
                     {ln.split(" ", 1)[0] for ln in _lines(log)}, 3.0)
    for line in _lines(log):
        fields = line.split(" ", 2)
        assert fields[1] == run
        assert isinstance(json.loads(fields[2]), dict)


# ── 2. heartbeat opt-in ────────────────────────────────────────────────────


def test_heartbeat_suppressed_unless_opt_in(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.observability.node_events import mo_node_emit
    script, log = _hook_script(tmp_path)
    monkeypatch.setenv("MINI_ORK_ON_EVENT", str(script))
    mo_node_emit("run-wired-hb", "hb1", "worker", "node_heartbeat")
    time.sleep(1.0)
    assert _lines(log) == []

    monkeypatch.setenv("MINI_ORK_ON_EVENT_HEARTBEATS", "1")
    mo_node_emit("run-wired-hb", "hb2", "worker", "node_heartbeat")
    assert _wait_for(lambda: _lines(log) != [], 3.0)
    assert _lines(log)[0].split(" ", 2)[0] == "node_heartbeat"


# ── 3. command arguments come first ────────────────────────────────────────


def test_command_with_arguments_runs(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.observability.node_events import mo_node_start
    script, log = _hook_script(tmp_path)
    monkeypatch.setenv("MINI_ORK_ON_EVENT", f"{script} --tag x")
    mo_node_start("run-wired-args", "n1", "implementer")
    assert _wait_for(lambda: _lines(log) != [], 3.0)
    assert _lines(log)[0] == "--tag x node_start"


# ── 4. invalid value: no exception, one warning, no value leak ─────────────


def test_invalid_value_warns_once(tmp_path: Path, monkeypatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    from mini_ork.observability.emit_hook import mo_emit_hook
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("MINI_ORK_ON_EVENT", f"{missing} --token SECRET42")
    mo_emit_hook("node_start", "r1", "{}")
    mo_emit_hook("node_end", "r1", "{}")
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert lines[0] == ("mini-ork: MINI_ORK_ON_EVENT is not an executable "
                        "command; event hook disabled")
    assert "SECRET42" not in err and str(missing) not in err


# ── 5. non-blocking, 5 s kill ──────────────────────────────────────────────


def test_non_blocking_spawn_and_kill(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.observability.emit_hook import mo_emit_hook
    from mini_ork.observability.node_events import mo_node_start
    slow = tmp_path / "slow-hook.sh"
    slow.write_text('#!/bin/sh\necho $$ > "$(dirname "$0")/slow.pid"\nsleep 10\n',
                    encoding="utf-8")
    slow.chmod(0o755)
    pidfile = tmp_path / "slow.pid"
    monkeypatch.setenv("MINI_ORK_ON_EVENT", str(slow))

    t0 = time.monotonic()
    assert mo_emit_hook("node_start", "run-slow", "{}") is None
    assert time.monotonic() - t0 < 0.5

    t1 = time.monotonic()
    assert mo_node_start("run-slow", "n1", "implementer") == 0
    assert time.monotonic() - t1 < 1.0

    assert _wait_for(lambda: pidfile.is_file(), 2.0)
    pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 8.0
    dead = False
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            dead = True
            break
        except PermissionError:
            pass
        time.sleep(0.1)
    assert dead, "sleeping hook was not killed and reaped"


# ── 6. recursive child events ──────────────────────────────────────────────


def test_recursive_child_event_fires_hook(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.orchestration.recursive import mo_recursive_emit_event
    script, log = _hook_script(tmp_path)
    monkeypatch.setenv("MINI_ORK_ON_EVENT", str(script))
    mo_recursive_emit_event("run-wired-child", event_type="child.completed",
                            payload_json='{"depth":1}')
    assert _wait_for(lambda: _lines(log) != [], 3.0)
    fields = _lines(log)[0].split(" ", 2)
    assert fields[0] == "child.completed"
    assert fields[1] == "run-wired-child"
    assert json.loads(fields[2]) == {"depth": 1}


# ── 7. run_finished on a terminal close ────────────────────────────────────


def _insert_task_run(db: Path, run: str, status: str = "executing",
                     verdict: str | None = None) -> None:
    now = int(time.time())
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "INSERT INTO task_runs(id, task_class, kickoff_path, status, verdict,"
            " created_at, updated_at) VALUES (?, 'wired_class', '/tmp/x.md', ?, ?, ?, ?)",
            (run, status, verdict, now, now))
        con.commit()
    finally:
        con.close()


def test_close_run_record_emits_run_finished(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.orchestration.run_reaper import close_run_record
    script, log = _hook_script(tmp_path)
    monkeypatch.setenv("MINI_ORK_ON_EVENT", str(script))
    db = Path(os.environ["MINI_ORK_DB"])
    run = "run-wired-reap"
    run_dir = tmp_path / "run-dir"
    run_dir.mkdir()
    _insert_task_run(db, run)

    assert close_run_record(str(db), run, run_dir, crashed=True) == "executing"
    assert _wait_for(lambda: _lines(log) != [], 3.0)
    payload = json.loads(_lines(log)[0].split(" ", 2)[2])
    assert payload == {"status": "failed", "verdict": None}

    # Second close on a terminal row changes nothing and emits nothing.
    assert close_run_record(str(db), run, run_dir, crashed=True) is None
    time.sleep(1.0)
    assert len([ln for ln in _lines(log) if ln.startswith("run_finished ")]) == 1


def test_run_finished_carries_verdict(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.orchestration.run_reaper import close_run_record
    script, log = _hook_script(tmp_path)
    monkeypatch.setenv("MINI_ORK_ON_EVENT", str(script))
    db = Path(os.environ["MINI_ORK_DB"])
    run = "run-wired-verdict"
    run_dir = tmp_path / "run-dir"
    run_dir.mkdir()
    _insert_task_run(db, run, verdict="ESCALATE")

    close_run_record(str(db), run, run_dir, crashed=True)
    assert _wait_for(lambda: _lines(log) != [], 3.0)
    payload = json.loads(_lines(log)[0].split(" ", 2)[2])
    assert payload["verdict"] == "ESCALATE"


# ── 8. unset never spawns ──────────────────────────────────────────────────


def test_no_spawn_when_env_unset(monkeypatch) -> None:
    from mini_ork.observability import emit_hook as eh
    from mini_ork.observability.node_events import mo_node_start, mo_run_emit
    from mini_ork.orchestration.recursive import mo_recursive_emit_event
    calls: list = []

    def _fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.delenv("MINI_ORK_ON_EVENT", raising=False)

    assert mo_node_start("run-wired-unset", "n1", "planner") == 0
    assert mo_run_emit("run-wired-unset", "run_classified") == 0
    mo_recursive_emit_event("run-wired-unset", event_type="child.started",
                            payload_json="{}")
    hook_spawns = [c for c in calls
                   if c[0] and c[0][0]
                   and os.path.basename(str(c[0][0][0])) not in ("gdate", "date")]
    assert hook_spawns == []
    assert eh.mo_emit_hook("node_start", "r", "{}") is None


def test_run_finished_fires_once_for_a_run_the_publisher_already_finished(
        tmp_path: Path, monkeypatch) -> None:
    """The usual published path: the publisher wrote 'published' mid-workflow,
    so the teardown finds the row terminal and must still emit, exactly once."""
    from mini_ork.orchestration.run_reaper import close_run_record
    script, log = _hook_script(tmp_path)
    monkeypatch.setenv("MINI_ORK_ON_EVENT", str(script))
    db = Path(os.environ["MINI_ORK_DB"])
    run = "run-wired-published"
    run_dir = tmp_path / "run-dir"
    run_dir.mkdir()
    _insert_task_run(db, run, status="published", verdict="APPROVE")

    assert close_run_record(str(db), run, run_dir, crashed=False, rc=0) is None
    assert _wait_for(lambda: _lines(log) != [], 3.0)
    payload = json.loads(_lines(log)[0].split(" ", 2)[2])
    assert payload == {"status": "published", "verdict": "APPROVE"}

    close_run_record(str(db), run, run_dir, crashed=False, rc=0)
    time.sleep(1.0)
    assert len([ln for ln in _lines(log) if ln.startswith("run_finished ")]) == 1


def test_inflight_cap_drops_events_beyond_the_limit(tmp_path: Path, monkeypatch) -> None:
    from mini_ork.observability import emit_hook
    script = tmp_path / "slow-hook.sh"
    script.write_text("#!/bin/sh\nsleep 3\n")
    script.chmod(0o755)
    monkeypatch.setenv("MINI_ORK_ON_EVENT", str(script))
    monkeypatch.setattr(emit_hook, "_INFLIGHT", threading.BoundedSemaphore(2))
    spawned = []
    real_popen = subprocess.Popen

    def counting_popen(*args, **kwargs):
        spawned.append(args[0])
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(emit_hook.subprocess, "Popen", counting_popen)
    for _ in range(5):
        emit_hook.mo_emit_hook("node_start", "run-cap", "{}")
    assert len(spawned) == 2
