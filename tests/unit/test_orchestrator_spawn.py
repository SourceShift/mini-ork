"""`mini-ork run` spawns the per-run watcher exactly once, and safely.

The watcher is detached and must outlive the dispatcher, so the spawn contract
is: the run's own policy is inherited, node identity is not, and nothing here
can change the run's exit code.
"""
from __future__ import annotations

import inspect
import os
import subprocess
import time

import pytest

from mini_ork.cli import main as cli_main
from mini_ork.orchestration import runner_orchestrator as orch


class _Popen:
    calls: list = []

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        _Popen.calls.append(self)


@pytest.fixture(autouse=True)
def _spawn_env(monkeypatch):
    # conftest disables the watcher suite-wide; these tests are the opt-in.
    monkeypatch.setenv("MO_ORCH_WATCH", "1")
    monkeypatch.delenv("MO_ORCH_WATCH_AUTONOMY", raising=False)
    monkeypatch.delenv("MINI_ORK_DRY_RUN", raising=False)
    monkeypatch.setattr(subprocess, "Popen", _Popen)
    _Popen.calls = []


def _run_dir(tmp_path):
    home = tmp_path / "home"
    run_dir = home / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    return home, run_dir


def test_spawn_argv_env_and_log(tmp_path):
    home, run_dir = _run_dir(tmp_path)
    os.environ.update({
        "MO_NODE_ID": "impl",
        "MO_NODE_TYPE": "implementer",
        "MINI_ORK_VENV_ACTIVE": "1",
        "CONCORD_PRINCIPAL": "run:1",
        "MINI_ORK_DRY_RUN": "1",
    })

    # Dry-run is checked in the parent env; clear it and spawn for real.
    assert cli_main._start_orchestrator_watch(str(tmp_path), str(home), "run-1",
                                             str(run_dir)) is False
    os.environ.pop("MINI_ORK_DRY_RUN", None)
    assert cli_main._start_orchestrator_watch(str(tmp_path), str(home), "run-1",
                                             str(run_dir)) is True

    assert _Popen.calls, "no spawn recorded"
    call = _Popen.calls[-1]
    assert call.argv[1:7] == ["-m", "mini_ork.cli.orchestrator_cmd", "watch",
                              "run-1", "--home", str(home)]
    assert call.kwargs["cwd"] == str(tmp_path)
    assert call.kwargs["start_new_session"] is True
    assert call.kwargs["stdin"] is subprocess.DEVNULL
    assert call.kwargs["stderr"] is subprocess.STDOUT

    env = call.kwargs["env"]
    assert env["MINI_ORK_RUN_ID"] == "run-1"
    assert env["MINI_ORK_HOME"] == str(home)
    assert env["MINI_ORK_PROJECT_HOME"] == str(home)
    assert env["MINI_ORK_RUN_DIR"] == str(run_dir)
    assert env["MINI_ORK_DB"] == str(home / "state.db")
    for name in cli_main._ORCH_WATCH_CHILD_UNSET:
        assert name not in env, name
    assert (run_dir / orch.WATCH_LOG_FILE).is_file()


@pytest.mark.parametrize("name,value", [
    ("MO_ORCH_WATCH", "0"),
    ("MO_RUN_ORCHESTRATOR", "0"),
    ("MO_ORCH_WATCH_AUTONOMY", "off"),
    ("MINI_ORK_DRY_RUN", "1"),
])
def test_spawn_is_skipped_when_disabled(tmp_path, monkeypatch, name, value):
    home, run_dir = _run_dir(tmp_path)
    monkeypatch.setenv(name, value)
    assert cli_main._start_orchestrator_watch(str(tmp_path), str(home), "run-1",
                                             str(run_dir)) is False
    assert _Popen.calls == []


def test_spawn_is_skipped_when_a_watcher_is_live(tmp_path):
    home, run_dir = _run_dir(tmp_path)
    orch.save_watch_state(run_dir, orch.WatchState(
        pid=os.getpid(), started_at=time.time(), last_tick=time.time(),
        tick_count=2))
    assert cli_main._start_orchestrator_watch(str(tmp_path), str(home), "run-1",
                                             str(run_dir)) is False
    assert _Popen.calls == []


def test_spawn_failure_is_contained(tmp_path, monkeypatch, capsys):
    home, run_dir = _run_dir(tmp_path)

    def _boom(*_args, **_kwargs):
        raise OSError("no fork for you")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert cli_main._start_orchestrator_watch(str(tmp_path), str(home), "run-1",
                                             str(run_dir)) is False
    assert "watcher not started" in capsys.readouterr().err


def test_spawn_happens_after_the_run_env_is_published():
    source = inspect.getsource(cli_main._run_lifecycle_impl)
    assert source.index("load_run_env(run_dir)") < source.index(
        "_start_orchestrator_watch(root, home, run_id, run_dir)")
