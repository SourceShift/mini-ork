"""worktree_guard — Python port of lib/worktree-guard.sh.

Faithful port of ``mo_wait_for_worker_quiescence``: the worktree-mutation
gate that blocks until the latest in-flight worker has either exited or
stopped writing to its log. Mirrors the shell logic in
``lib/worktree-guard.sh`` exactly — same 1Hz polling cadence, same mtime
string comparison, same canonical stderr message, same exit-code contract.

Co-existence model (strangler-fig): ``lib/worktree-guard.sh`` stays
byte-identical. This port gives Python callers an in-process target and
gives ``tests/unit/test_worktree_guard_py.py`` a stable surface for parity
verification against the LIVE bash subprocess (no mocks, no hardcoded
outputs).

Env knobs (bash reads these at function entry; the Python port honours them
when the explicit kwargs are ``None``):
  ``MO_WORKTREE_GUARD_MAX_WAIT_S``  → ``max_wait_s``       (default ``30``)
  ``MO_WORKTREE_GUARD_STABLE_S``    → ``stable_window_s``  (default ``5``)

Returns ``(rc, stderr)`` — a 2-tuple matching the bash function's contract.

  ``rc = 0``  quiescent — no run dir, no ``iter-N/worker.pid``, empty pid,
                dead pid, worker exited mid-wait, OR worker.log mtime
                unchanged for ``stable_window_s`` consecutive 1-second ticks.
  ``rc = 1``  still active past ``max_wait_s`` — stderr emits the canonical
                ``[worktree-guard] worker pid=<pid> still active after
                <N>s — caller to decide`` and the caller decides whether
                to defer or proceed.

The stderr string uses an em-dash (U+2014) — must match bash byte-for-byte.
"""
from __future__ import annotations

import glob
import os
import time


def _find_latest_pid_file(epic_run_dir: str) -> str | None:
    """Mirror ``ls -1t "$epic_run_dir"/iter-*/worker.pid | head -1``.

    Returns the most-recently-modified matching path, or ``None`` if no
    ``iter-N/worker.pid`` file exists under ``epic_run_dir``. Bash's
    ``ls -1t`` orders by mtime, newest first; ``head -1`` picks the first.
    """
    pattern = os.path.join(epic_run_dir, "iter-*", "worker.pid")
    matches = glob.glob(pattern)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _log_mtime(log_path: str) -> int:
    """Mirror ``stat -f %m`` / ``stat -c %Y`` — epoch seconds, 0 if missing.

    The bash source chains ``stat -f %m ... || stat -c %Y ... || echo 0``;
    any failure (missing file, unreadable, no stat binary) collapses to ``0``.
    Truncating the float ``os.path.getmtime`` to ``int`` matches the bash
    integer-string comparison (``[ "$cur_mtime" = "$last_mtime" ]``).
    """
    if not os.path.isfile(log_path):
        return 0
    try:
        return int(os.path.getmtime(log_path))
    except OSError:
        return 0


def wait_for_worker_quiescence(
    epic_run_dir: str,
    max_wait_s: int | None = None,
    stable_window_s: int | None = None,
) -> tuple[int, str]:
    """Port of ``mo_wait_for_worker_quiescence``. Returns ``(rc, stderr)``.

    Polls worker.log mtime at 1Hz until ``stable_window_s`` consecutive
    ticks show no change, the worker exits mid-wait, or ``max_wait_s``
    elapses. Reproduces bash semantics: missing dir → 0; no
    ``iter-N/worker.pid`` → 0; pid missing or dead → 0; else watch mtime.
    Storing mtimes as integer-second strings mirrors the bash string
    comparison line-for-line.
    """
    if max_wait_s is None:
        max_wait_s = int(os.environ.get("MO_WORKTREE_GUARD_MAX_WAIT_S", "30"))
    if stable_window_s is None:
        stable_window_s = int(os.environ.get("MO_WORKTREE_GUARD_STABLE_S", "5"))

    if not os.path.isdir(epic_run_dir):
        return 0, ""

    latest_pid_file = _find_latest_pid_file(epic_run_dir)
    if not latest_pid_file:
        return 0, ""

    try:
        with open(latest_pid_file) as f:
            worker_pid_str = f.read().strip()
    except OSError:
        worker_pid_str = ""

    if not worker_pid_str:
        return 0, ""

    try:
        worker_pid = int(worker_pid_str)
    except ValueError:
        return 0, ""

    try:
        os.kill(worker_pid, 0)
    except OSError:
        return 0, ""

    worker_log = os.path.join(os.path.dirname(latest_pid_file), "worker.log")

    elapsed = 0
    last_mtime = ""
    stable_for = 0
    while elapsed < max_wait_s:
        try:
            os.kill(worker_pid, 0)
        except OSError:
            return 0, ""
        cur_mtime_str = str(_log_mtime(worker_log))
        if cur_mtime_str == last_mtime:
            stable_for += 1
            if stable_for >= stable_window_s:
                return 0, ""
        else:
            stable_for = 0
            last_mtime = cur_mtime_str
        time.sleep(1)
        elapsed += 1

    stderr = (
        f"[worktree-guard] worker pid={worker_pid} still active after "
        f"{max_wait_s}s — caller to decide"
    )
    return 1, stderr
