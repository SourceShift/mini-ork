"""Does a node timeout reap commands that escaped the leader's process group?

The process-group kill alone is not enough in practice: the Claude CLI runs
each Bash tool command in its *own* process group, so those commands — and
everything under them — survive a kill of the leader's group and reparent to
pid 1. The live failure this pins down: a ``cargo build`` the agent started
held its build-directory lock for 57 minutes after its node had timed out.

``test_timeout_kills_a_grandchild_in_its_own_process_group`` is THE assertion:
the grandchild is started with ``start_new_session=True``, exactly the axis the
older ``test_timeout_kills_the_whole_process_group`` cannot see. Everything else
here guards the edges of ``_descendant_pids`` and the self-kill hazard.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from mini_ork.dispatch import core
from mini_ork.dispatch.core import spawn_local

# The grandchild writes its own pid, then sleeps well past any timeout. The
# write is flushed+fsynced so the test can read the pid as soon as it appears.
_GRANDCHILD = (
    "import os, sys, time\n"
    "with open(sys.argv[1], 'w') as f:\n"
    "    f.write(str(os.getpid()))\n"
    "    f.flush()\n"
    "    os.fsync(f.fileno())\n"
    "time.sleep(60)\n"
)


def _child_script(pidfile: str) -> str:
    """A parent that starts one grandchild in its OWN session, then sleeps."""
    return (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {_GRANDCHILD!r}, {pidfile!r}],"
        " start_new_session=True)\n"
        "time.sleep(60)\n"
    )


def _await_file(path: Path, budget: float) -> None:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return
        time.sleep(0.02)
    raise AssertionError(f"{path} was not written within {budget}s")


def _is_dead(pid: int, budget: float) -> bool:
    """True once ``pid`` is gone; polls because reaping is asynchronous."""
    deadline = time.monotonic() + budget
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _force_kill(pid: int) -> None:
    """Best-effort cleanup so a failing assertion cannot leak a sleeper."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def test_timeout_kills_a_grandchild_in_its_own_process_group(tmp_path):
    """The orphan the group kill misses: a command the agent ran in its own
    process group (the Claude CLI's per-Bash-tool group) must be dead within 2 s
    of the node timeout, not reparented to launchd and left burning CPU."""
    pidfile = tmp_path / "grandchild.pid"
    rc, _, _ = spawn_local(
        [sys.executable, "-c", _child_script(str(pidfile))],
        stdin="",
        timeout=5.0,
        env={},
        cwd=None,
    )
    assert rc == 124
    _await_file(pidfile, 5.0)
    grandchild = int(pidfile.read_text())
    try:
        assert _is_dead(grandchild, 2.0), (
            f"grandchild {grandchild} started a new session and survived the "
            f"node timeout — the orphan bug"
        )
    finally:
        _force_kill(grandchild)


def test_descendant_pids_of_a_childless_process_is_empty():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert core._descendant_pids(proc.pid) == []
    finally:
        proc.kill()
        proc.wait(timeout=5)


@pytest.mark.parametrize(
    "exc",
    [OSError("ps: not found"), subprocess.TimeoutExpired("ps", 5)],
    ids=["os-error", "timeout"],
)
def test_descendant_pids_is_fail_soft_when_ps_fails(monkeypatch, exc):
    """A diagnostic probe that hangs or raises would be worse than the orphan it
    is meant to catch, so any failure must degrade to ``[]``."""

    def boom(*_args, **_kwargs):
        raise exc

    # Scoped to the core module so no unrelated subprocess use is perturbed; the
    # real TimeoutExpired is kept so the fail-soft except clause still resolves.
    stub = types.SimpleNamespace(run=boom, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(core, "subprocess", stub)
    assert core._descendant_pids(os.getpid()) == []


def test_timeout_never_signals_our_own_process_group(tmp_path, monkeypatch):
    """[c:1]: the sweep must never signal this process's own group. Records
    every ``killpg`` and asserts our pgid is absent — while still forwarding the
    real signal for other groups so the escaped grandchild is actually reaped."""
    pidfile = tmp_path / "grandchild.pid"
    calls: list[int] = []
    real_killpg = os.killpg

    def record(pgid, sig):
        calls.append(pgid)
        if pgid != os.getpgrp():  # never really signal ourselves from a test
            real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", record)
    proc = subprocess.Popen(
        [sys.executable, "-c", _child_script(str(pidfile))],
        start_new_session=True,
    )
    # Wait for the grandchild so the descendant sweep actually runs.
    _await_file(pidfile, 5.0)
    grandchild = int(pidfile.read_text())
    try:
        core._terminate_process_group(proc)
    finally:
        proc.wait(timeout=5)
        # Clean up via the real signal: os.killpg is patched to `record` here.
        try:
            real_killpg(os.getpgid(grandchild), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    assert calls, "the leader's own group must still be signalled"
    assert os.getpgrp() not in calls
    assert grandchild in calls, "the escaped group was not swept"
