"""Standalone unit tests for ``mini_ork.vcs.worktree_guard``.

Replaces the bash-parity gate (subprocess round-trips through
``lib/worktree-guard.sh``) as part of the bash→Python migration: the Python
port is now the sole implementation exercised here, so coverage no longer
shells out to bash to run the shell function as an oracle — it asserts the
port's behaviour directly. The 1Hz poll loop (``time.sleep(1)``) and the
worker-log mtime reads are stubbed via ``monkeypatch`` so every case runs in
milliseconds instead of real wall-clock seconds.

Real child processes (via ``subprocess.Popen``) are still spawned for a
handful of cases — the port calls ``os.kill(pid, 0)`` to probe liveness, and
a real OS pid is the only faithful way to exercise "alive" vs "dead" without
reimplementing the kernel's process table. This is test-fixture plumbing,
not the SUT shelling out: ``worktree_guard.py`` itself never spawns a
subprocess (no ``source``, no ``bash -c``, no ``Popen``) — the whole surface
is ``os``, ``glob``, and ``time``.

Coverage mirrors and exceeds the retired parity suite's seven cases:
  * no run dir / no iter-N pid file / empty pid file / non-numeric pid file
    → rc 0, stderr ""
  * dead pid (spawn-and-exit helper) → rc 0
  * worker exits mid-wait (kill succeeds once, then fails) → rc 0
  * alive + stable log (mtime constant) → rc 0
  * alive + no log file at all (mtime treated as 0, constant) → rc 0
  * alive + writing log (mtime strictly advancing) → rc 1, canonical stderr
  * env-var overrides for both knobs, explicit-kwarg precedence over env,
    and the bash-mirrored defaults (30 / 5) — all exercised behaviourally
    via a monotonically increasing / constant fake mtime rather than by
    inspecting source text.
Plus direct unit coverage of the two private helpers, ``_find_latest_pid_file``
(mtime-newest selection across multiple ``iter-*`` dirs) and ``_log_mtime``
(missing-file → 0, present-file → truncated int epoch).
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mini_ork.vcs import worktree_guard as wg

# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def alive_worker():
    """Long-running child process; terminated at teardown."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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
                proc.wait(timeout=2)


def _make_dead_pid() -> int:
    """Spawn-and-reap a child; its pid is OS-valid and guaranteed dead.

    Synthetic high pids (e.g. ``999_999_999``) are not safe on macOS —
    ``kill`` returns ``EINVAL`` for pids outside ``kern.maxproc`` rather than
    "no such process" — so a real spawn-and-exit is used instead.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait()
    return proc.pid


def _write_layout(
    epic_dir: Path,
    worker_pid: int | str | None,
    include_log: bool = True,
    iter_name: str = "iter-1",
) -> Path:
    """Build ``epic_dir/<iter_name>`` with an optional ``worker.pid``/``worker.log``."""
    iter_dir = epic_dir / iter_name
    iter_dir.mkdir(parents=True)
    if worker_pid is not None:
        (iter_dir / "worker.pid").write_text(str(worker_pid))
    if include_log:
        (iter_dir / "worker.log").write_text("")
    return iter_dir


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the port's 1Hz poll cadence to instant for fast tests."""
    monkeypatch.setattr(wg.time, "sleep", lambda *_a, **_k: None)


# ─────────────────────────────────────────────────────────────────────────────
# _find_latest_pid_file
# ─────────────────────────────────────────────────────────────────────────────


class TestFindLatestPidFile:
    def test_no_iter_dirs_returns_none(self, tmp_path):
        assert wg._find_latest_pid_file(str(tmp_path)) is None

    def test_iter_dir_without_pid_file_returns_none(self, tmp_path):
        (tmp_path / "iter-1").mkdir()
        assert wg._find_latest_pid_file(str(tmp_path)) is None

    def test_single_match_returns_that_path(self, tmp_path):
        iter_dir = tmp_path / "iter-1"
        iter_dir.mkdir()
        pid_file = iter_dir / "worker.pid"
        pid_file.write_text("123")
        assert wg._find_latest_pid_file(str(tmp_path)) == str(pid_file)

    def test_multiple_matches_returns_most_recently_modified(self, tmp_path):
        older = tmp_path / "iter-1"
        newer = tmp_path / "iter-2"
        older.mkdir()
        newer.mkdir()
        older_pid = older / "worker.pid"
        newer_pid = newer / "worker.pid"
        older_pid.write_text("111")
        newer_pid.write_text("222")
        # Force explicit, unambiguous mtimes (filesystem write order alone
        # can be sub-second and flaky on fast disks).
        os.utime(older_pid, (1_000_000, 1_000_000))
        os.utime(newer_pid, (2_000_000, 2_000_000))
        assert wg._find_latest_pid_file(str(tmp_path)) == str(newer_pid)


# ─────────────────────────────────────────────────────────────────────────────
# _log_mtime
# ─────────────────────────────────────────────────────────────────────────────


class TestLogMtime:
    def test_missing_file_returns_zero(self, tmp_path):
        assert wg._log_mtime(str(tmp_path / "nope.log")) == 0

    def test_existing_file_returns_truncated_int_epoch(self, tmp_path):
        log = tmp_path / "worker.log"
        log.write_text("")
        os.utime(log, (1_700_000_000.9, 1_700_000_000.9))
        assert wg._log_mtime(str(log)) == 1_700_000_000


# ─────────────────────────────────────────────────────────────────────────────
# wait_for_worker_quiescence — early-return branches (no loop entered)
# ─────────────────────────────────────────────────────────────────────────────


class TestEarlyReturns:
    def test_no_run_dir_returns_0(self, tmp_path):
        missing = str(tmp_path / "does-not-exist")
        rc, stderr = wg.wait_for_worker_quiescence(missing)
        assert (rc, stderr) == (0, "")

    def test_no_iter_pid_file_returns_0(self, tmp_path):
        epic = tmp_path / "epic"
        (epic / "iter-1").mkdir(parents=True)
        rc, stderr = wg.wait_for_worker_quiescence(str(epic))
        assert (rc, stderr) == (0, "")

    def test_empty_pid_file_returns_0(self, tmp_path):
        epic = tmp_path / "epic"
        _write_layout(epic, worker_pid="")
        rc, stderr = wg.wait_for_worker_quiescence(str(epic))
        assert (rc, stderr) == (0, "")

    def test_non_numeric_pid_file_returns_0(self, tmp_path):
        epic = tmp_path / "epic"
        _write_layout(epic, worker_pid="not-a-pid")
        rc, stderr = wg.wait_for_worker_quiescence(str(epic))
        assert (rc, stderr) == (0, "")

    def test_dead_pid_returns_0(self, tmp_path):
        epic = tmp_path / "epic"
        _write_layout(epic, worker_pid=_make_dead_pid())
        rc, stderr = wg.wait_for_worker_quiescence(str(epic))
        assert (rc, stderr) == (0, "")

    def test_worker_exits_mid_wait_returns_0(self, monkeypatch, tmp_path):
        """Alive at the pre-loop check, dead by the first in-loop check."""
        epic = tmp_path / "epic"
        _write_layout(epic, worker_pid=12345)
        _no_sleep(monkeypatch)

        calls = {"n": 0}

        def fake_kill(pid: int, sig: int) -> None:
            calls["n"] += 1
            if calls["n"] >= 2:
                raise ProcessLookupError
            return None

        monkeypatch.setattr(wg.os, "kill", fake_kill)
        rc, stderr = wg.wait_for_worker_quiescence(
            str(epic), max_wait_s=5, stable_window_s=5
        )
        assert (rc, stderr) == (0, "")
        assert calls["n"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# wait_for_worker_quiescence — the poll loop
# ─────────────────────────────────────────────────────────────────────────────


class TestPollLoop:
    def test_alive_stable_returns_0(self, monkeypatch, alive_worker, tmp_path):
        epic = tmp_path / "epic"
        _write_layout(epic, worker_pid=alive_worker)  # mtime never touched → constant
        _no_sleep(monkeypatch)
        rc, stderr = wg.wait_for_worker_quiescence(
            str(epic), max_wait_s=5, stable_window_s=1
        )
        assert (rc, stderr) == (0, "")

    def test_alive_no_log_file_returns_0(self, monkeypatch, alive_worker, tmp_path):
        """No worker.log at all → mtime reads as a constant 0 → stabilizes."""
        epic = tmp_path / "epic"
        _write_layout(epic, worker_pid=alive_worker, include_log=False)
        _no_sleep(monkeypatch)
        rc, stderr = wg.wait_for_worker_quiescence(
            str(epic), max_wait_s=5, stable_window_s=1
        )
        assert (rc, stderr) == (0, "")

    def test_alive_writing_returns_1_canonical_stderr(
        self, monkeypatch, alive_worker, tmp_path
    ):
        epic = tmp_path / "epic"
        _write_layout(epic, worker_pid=alive_worker)
        _no_sleep(monkeypatch)
        counter = itertools.count()
        monkeypatch.setattr(wg, "_log_mtime", lambda _path: next(counter))

        rc, stderr = wg.wait_for_worker_quiescence(
            str(epic), max_wait_s=3, stable_window_s=2
        )
        assert rc == 1
        assert stderr == (
            f"[worktree-guard] worker pid={alive_worker} still active after "
            "3s — caller to decide"
        )
        # Em-dash (U+2014), not a hyphen — must match the bash source byte-for-byte.
        assert "—" in stderr


# ─────────────────────────────────────────────────────────────────────────────
# wait_for_worker_quiescence — env-var knobs and defaults
# ─────────────────────────────────────────────────────────────────────────────


class TestEnvAndDefaults:
    def test_env_override_max_wait_respected(
        self, monkeypatch, alive_worker, tmp_path
    ):
        epic = tmp_path / "epic"
        _write_layout(epic, worker_pid=alive_worker)
        _no_sleep(monkeypatch)
        counter = itertools.count()
        monkeypatch.setattr(wg, "_log_mtime", lambda _path: next(counter))
        monkeypatch.setenv("MO_WORKTREE_GUARD_MAX_WAIT_S", "1")
        monkeypatch.setenv("MO_WORKTREE_GUARD_STABLE_S", "1")

        rc, stderr = wg.wait_for_worker_quiescence(str(epic))
        assert rc == 1
        assert "after 1s" in stderr
        assert "after 30s" not in stderr

    def test_env_override_stable_window_respected(
        self, monkeypatch, alive_worker, tmp_path
    ):
        epic = tmp_path / "epic"
        _write_layout(epic, worker_pid=alive_worker)
        _no_sleep(monkeypatch)
        monkeypatch.delenv("MO_WORKTREE_GUARD_MAX_WAIT_S", raising=False)
        monkeypatch.setenv("MO_WORKTREE_GUARD_STABLE_S", "1")

        calls: list[int] = []

        def fake_log_mtime(_path: str) -> int:
            calls.append(1)
            return 42  # constant mtime

        monkeypatch.setattr(wg, "_log_mtime", fake_log_mtime)
        rc, stderr = wg.wait_for_worker_quiescence(str(epic), max_wait_s=10)
        assert (rc, stderr) == (0, "")
        # iter1: baseline (mismatch vs ""); iter2: matches → stable_for=1 >= 1 → return.
        # A default stable_window of 5 would need 6 calls instead.
        assert len(calls) == 2

    def test_explicit_kwargs_take_precedence_over_env(
        self, monkeypatch, alive_worker, tmp_path
    ):
        epic = tmp_path / "epic"
        _write_layout(epic, worker_pid=alive_worker)
        _no_sleep(monkeypatch)
        counter = itertools.count()
        monkeypatch.setattr(wg, "_log_mtime", lambda _path: next(counter))
        monkeypatch.setenv("MO_WORKTREE_GUARD_MAX_WAIT_S", "99")
        monkeypatch.setenv("MO_WORKTREE_GUARD_STABLE_S", "99")

        rc, stderr = wg.wait_for_worker_quiescence(
            str(epic), max_wait_s=2, stable_window_s=1
        )
        assert rc == 1
        assert "after 2s" in stderr
        assert "after 99s" not in stderr

    def test_defaults_are_30_and_5_when_unset(
        self, monkeypatch, alive_worker, tmp_path
    ):
        """No kwargs, no env → bash-mirrored defaults of 30s max-wait / 5s stable."""
        epic = tmp_path / "epic"
        _write_layout(epic, worker_pid=alive_worker)
        _no_sleep(monkeypatch)
        monkeypatch.delenv("MO_WORKTREE_GUARD_MAX_WAIT_S", raising=False)
        monkeypatch.delenv("MO_WORKTREE_GUARD_STABLE_S", raising=False)
        counter = itertools.count()
        monkeypatch.setattr(wg, "_log_mtime", lambda _path: next(counter))

        rc, stderr = wg.wait_for_worker_quiescence(str(epic))
        assert rc == 1
        assert "after 30s" in stderr

    def test_default_stable_window_is_5_when_unset(
        self, monkeypatch, alive_worker, tmp_path
    ):
        epic = tmp_path / "epic"
        _write_layout(epic, worker_pid=alive_worker)
        _no_sleep(monkeypatch)
        monkeypatch.delenv("MO_WORKTREE_GUARD_MAX_WAIT_S", raising=False)
        monkeypatch.delenv("MO_WORKTREE_GUARD_STABLE_S", raising=False)

        calls: list[int] = []

        def fake_log_mtime(_path: str) -> int:
            calls.append(1)
            return 7  # constant mtime

        monkeypatch.setattr(wg, "_log_mtime", fake_log_mtime)
        rc, stderr = wg.wait_for_worker_quiescence(str(epic), max_wait_s=30)
        assert (rc, stderr) == (0, "")
        # iter1 baseline (mismatch), iters 2-6 matching → stable_for reaches 5 on
        # the 6th call. A stable_window other than 5 would need a different count.
        assert len(calls) == 6
