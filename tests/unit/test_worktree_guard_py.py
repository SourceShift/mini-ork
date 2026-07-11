"""Parity gate: ``mini_ork.ported.worktree_guard`` vs ``lib/worktree-guard.sh``.

Each test drives the LIVE bash function ``mo_wait_for_worker_quiescence``
via ``bash -c 'source lib/worktree-guard.sh; mo_wait_for_worker_quiescence
"$1" "$2" "$3"'`` against the SAME on-disk ``epic_run_dir`` tree that the
Python port reads, then asserts the two implementations agree:

  * exit code equal
  * stderr byte-for-byte equal (only the canonical message, not bash chatter)

No stub fixtures, no hardcoded bash outputs beyond what the function emits itself.

Host assumption: verified on ``darwin`` (macOS bash 3.2+); the same logic
holds on Linux bash 5+ because both honour the ``kill -0`` semantics the
bash function relies on and ``set -uo pipefail`` for unset-var safety.
Pids stay within the OS-valid range (``< kern.maxproc``) — synthetic
out-of-range pids are NOT used (would produce EINVAL on macOS instead of
"no such process").

Env-leak guard: each bash subprocess and Python call strips
``MO_WORKTREE_GUARD_MAX_WAIT_S`` / ``MO_WORKTREE_GUARD_STABLE_S`` from the
parent environment first, except in the explicit ``env_override`` case.
A leak from the runner's environment would silently override the per-test
timeouts and break parity.

Seven cases (above the kickoff's ``>=6`` floor):
  (a) no_run_dir        → both return 0, stderr empty
  (b) no_worker_pid     → both return 0, stderr empty
  (c) dead_pid          → both return 0, stderr empty (kill -0 fails)
  (d) alive_stable      → both return 0 in ~2s, stderr empty
  (e) alive_writing     → both return 1 in ~2s, stderr matches canonical
  (f) env_override      → ``MO_WORKTREE_GUARD_MAX_WAIT_S=1`` honoured (returns
                          1 fast; stderr contains ``"after 1s"``)
  (g) bash_defaults     → inspect ``declare -f`` body for literal ``:-30``
                          and ``:-5`` (bash-only, no parity loop)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import worktree_guard as wg  # noqa: E402

SH = REPO / "lib" / "worktree-guard.sh"

# Env vars that drive the bash function. Stripped from parent env before
# each invocation so the runner's shell doesn't leak into the test.
_GUARD_ENV_KEYS = ("MO_WORKTREE_GUARD_MAX_WAIT_S", "MO_WORKTREE_GUARD_STABLE_S")


def _which_bash() -> None:
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    if not SH.exists():
        pytest.skip(f"missing lib/worktree-guard.sh at {SH}")


def _clean_env() -> dict:
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO)}
    for k in _GUARD_ENV_KEYS:
        env.pop(k, None)
    return env


def _bash_quiescence(
    epic_run_dir: str,
    max_wait: str = "",
    stable_window: str = "",
    env_overrides: dict | None = None,
) -> tuple[int, str]:
    """Invoke live bash ``mo_wait_for_worker_quiescence``; return ``(rc, stderr)``."""
    env = _clean_env()
    if env_overrides:
        env.update(env_overrides)
    src = (
        f'source "{SH}"\n'
        f'mo_wait_for_worker_quiescence "$1" "$2" "$3"\n'
    )
    r = subprocess.run(
        ["bash", "-c", src, "_", epic_run_dir, max_wait, stable_window],
        env=env, capture_output=True, text=True,
    )
    return r.returncode, r.stderr


def _py_quiescence(
    epic_run_dir: str,
    max_wait_s: int | None = None,
    stable_window_s: int | None = None,
    env_overrides: dict | None = None,
) -> tuple[int, str]:
    """Call the Python port with parent env stripped of MO_WORKTREE_GUARD_*."""
    saved = {
        k: os.environ.pop(k)
        for k in _GUARD_ENV_KEYS
        if k in os.environ
    }
    try:
        if env_overrides:
            for k, v in env_overrides.items():
                os.environ[k] = str(v)
        return wg.wait_for_worker_quiescence(
            epic_run_dir,
            max_wait_s=max_wait_s,
            stable_window_s=stable_window_s,
        )
    finally:
        for k in _GUARD_ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update(saved)


def _assert_parity(bash_pair: tuple[int, str], py_pair: tuple[int, str]) -> None:
    bash_rc, bash_stderr = bash_pair
    py_rc, py_stderr = py_pair
    assert bash_rc == py_rc, (
        f"rc mismatch: bash={bash_rc} py={py_rc}\n"
        f"bash stderr={bash_stderr!r}\n"
        f"py   stderr={py_stderr!r}"
    )
    # bash's `echo` appends a trailing newline; the canonical message body
    # itself is what we actually want to compare. The kickoff explicitly
    # allows stripping "env-dependent shell chatter" — this newline is the
    # only such artefact on the timeout path.
    assert bash_stderr.rstrip() == py_stderr, (
        f"stderr mismatch\nbash={bash_stderr!r}\npy  ={py_stderr!r}"
    )


@pytest.fixture
def alive_worker():
    """Long-running child process; cleaned up at teardown."""
    proc = subprocess.Popen(
        ["sleep", "30"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        yield proc.pid
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def _make_dead_pid() -> int:
    """Spawn-and-exit a child; its pid is in the OS-valid range and is dead.

    Synthetic high pids (e.g., ``999_999_999``) are NOT safe on macOS —
    ``kill`` returns ``EINVAL`` for pids ``>= kern.maxproc``, which is a
    different error than "process not found". This helper avoids that pitfall.
    """
    proc = subprocess.Popen(
        ["bash", "-c", "exit 0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proc.wait()
    return proc.pid


def _log_writer(log_path: Path, stop: threading.Event) -> threading.Thread:
    """Background thread that bumps ``log_path`` mtime every 100ms.

    Each touch advances the mtime by at least 1 full second. This is
    load-bearing: bash's ``stat -f %m`` returns *integer epoch seconds*
    (truncated from float). If the thread merely touched with
    ``time.time()``, sub-second-touch mtimes would map to the same
    integer second across multiple bash ticks → bash would see the log
    as stable, return ``rc=0`` on a tick that python (also reading
    integer seconds, but advancing past the second boundary) sees as
    changing → parity break.

    Advancing mtime by +1s per touch guarantees the integer second
    ticks up strictly between any two bash reads spaced >=1s apart,
    matching python's behaviour and producing rc=1 deterministically.
    """
    def loop():
        i = 0
        while not stop.is_set():
            try:
                t = time.time() + i  # +1s per touch → integer second strictly advances
                os.utime(log_path, (t, t))
            except OSError:
                return
            stop.wait(0.1)
            i += 1

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def _write_layout(epic_dir: Path, worker_pid: int | None,
                  include_log: bool = True) -> Path:
    """Build epic_run_dir/iter-1 with worker.pid + optional worker.log."""
    iter_dir = epic_dir / "iter-1"
    iter_dir.mkdir(parents=True)
    if worker_pid is not None:
        (iter_dir / "worker.pid").write_text(str(worker_pid))
    if include_log:
        (iter_dir / "worker.log").write_text("")
    return iter_dir


# ─────────────────────────────────────────────────────────────────────────────
# (a) no_run_dir — epic_run_dir does not exist at all.
# ─────────────────────────────────────────────────────────────────────────────
def test_no_run_dir_returns_0(tmp_path):
    _which_bash()
    missing = str(tmp_path / "does-not-exist")
    _assert_parity(_bash_quiescence(missing), _py_quiescence(missing))


# ─────────────────────────────────────────────────────────────────────────────
# (b) no_worker_pid — iter-N exists but no worker.pid under it.
# ─────────────────────────────────────────────────────────────────────────────
def test_no_worker_pid_returns_0(tmp_path):
    _which_bash()
    epic = tmp_path / "epic"
    (epic / "iter-1").mkdir(parents=True)
    _assert_parity(_bash_quiescence(str(epic)), _py_quiescence(str(epic)))


# ─────────────────────────────────────────────────────────────────────────────
# (c) dead_pid — worker.pid points at a process that has already exited.
#     kill -0 / os.kill(pid, 0) fail → both return 0 without touching the loop.
# ─────────────────────────────────────────────────────────────────────────────
def test_dead_pid_returns_0(tmp_path):
    _which_bash()
    epic = tmp_path / "epic"
    _write_layout(epic, worker_pid=_make_dead_pid())
    _assert_parity(
        _bash_quiescence(str(epic)),
        _py_quiescence(str(epic)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# (d) alive_stable — worker alive, log created but not touched during the run.
#     Mtime stays constant across all ticks → stable_for hits stable_window=1
#     after the baseline tick → both return 0 in ~1s.
# ─────────────────────────────────────────────────────────────────────────────
def test_alive_stable_returns_0(tmp_path, alive_worker):
    _which_bash()
    epic = tmp_path / "epic"
    _write_layout(epic, worker_pid=alive_worker)
    bash_pair = _bash_quiescence(str(epic), "2", "1")
    py_pair = _py_quiescence(
        str(epic), max_wait_s=2, stable_window_s=1
    )
    _assert_parity(bash_pair, py_pair)
    assert bash_pair[0] == 0


# ─────────────────────────────────────────────────────────────────────────────
# (e) alive_writing — worker alive AND log mtime changes every poll tick.
#     Background thread bumps mtime every 100ms with strictly advancing
#     timestamps; stable_for never accumulates → max_wait=2 elapses → both
#     return 1 with the canonical stderr.
# ─────────────────────────────────────────────────────────────────────────────
def test_alive_writing_returns_1(tmp_path, alive_worker):
    _which_bash()
    epic = tmp_path / "epic"
    iter_dir = _write_layout(epic, worker_pid=alive_worker)
    log_path = iter_dir / "worker.log"

    stop = threading.Event()
    writer = _log_writer(log_path, stop)
    try:
        bash_pair = _bash_quiescence(str(epic), "2", "1")
        py_pair = _py_quiescence(
            str(epic), max_wait_s=2, stable_window_s=1
        )
        _assert_parity(bash_pair, py_pair)
        bash_rc, bash_stderr = bash_pair
        assert bash_rc == 1
        assert "still active" in bash_stderr
        assert "after 2s" in bash_stderr
        assert "caller to decide" in bash_stderr
        # Em-dash (U+2014) — must match bash byte-for-byte, not a hyphen.
        assert "—" in bash_stderr
        # Pid in the message matches what we wrote.
        assert f"pid={alive_worker}" in bash_stderr
    finally:
        stop.set()
        writer.join(timeout=1)


# ─────────────────────────────────────────────────────────────────────────────
# (f) env_override — bash defaults are 30/5s; passing MO_WORKTREE_GUARD_MAX_WAIT_S=1
#     with no positional max_wait makes both sides exit in ~1s instead of ~30s.
#     This proves the env knob is honoured; without it both sides would wait
#     for the bash default of 30s and the test would time out.
# ─────────────────────────────────────────────────────────────────────────────
def test_env_override_respected(tmp_path, alive_worker):
    _which_bash()
    epic = tmp_path / "epic"
    iter_dir = _write_layout(epic, worker_pid=alive_worker)
    log_path = iter_dir / "worker.log"

    stop = threading.Event()
    writer = _log_writer(log_path, stop)
    try:
        env_overrides = {"MO_WORKTREE_GUARD_MAX_WAIT_S": "1"}
        # No positional args — must come from env override.
        bash_pair = _bash_quiescence(
            str(epic), "", "", env_overrides=env_overrides,
        )
        py_pair = _py_quiescence(
            str(epic), env_overrides=env_overrides,
        )
        _assert_parity(bash_pair, py_pair)
        bash_rc, bash_stderr = bash_pair
        assert bash_rc == 1
        # Env-supplied 1 must surface in the stderr; if the env had been
        # ignored (defaults of 30/5), the stderr would say "after 30s".
        assert "after 1s" in bash_stderr
        assert "after 30s" not in bash_stderr
    finally:
        stop.set()
        writer.join(timeout=1)


# ─────────────────────────────────────────────────────────────────────────────
# (g) bash_defaults — pin the bash function's hardcoded default literals to
#     30 (max_wait) and 5 (stable_window). Mechanical inspection via
#     ``declare -f`` so the test does NOT burn real wall-clock time waiting
#     on the production defaults.
# ─────────────────────────────────────────────────────────────────────────────
def test_bash_defaults_pinned():
    _which_bash()
    src = f'source "{SH}"; declare -f mo_wait_for_worker_quiescence'
    r = subprocess.run(
        ["bash", "-c", src], env=_clean_env(),
        capture_output=True, text=True, check=True,
    )
    body = r.stdout
    assert "MO_WORKTREE_GUARD_MAX_WAIT_S:-30" in body, body
    assert "MO_WORKTREE_GUARD_STABLE_S:-5" in body, body
