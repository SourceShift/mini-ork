"""RBF-25 — reaping a dead dispatcher also stops the agents it was driving.

Agents are spawned detached (``dispatch/core.py`` uses ``start_new_session``),
so a dispatcher killed without teardown leaves them writing to the run dir long
after the run is declared dead. Two halves of the fix are pinned here:

  * ``dispatch/core.py`` records ``"<pgid> <unix start ts>"`` in
    ``<run_dir>/.agent-pgids`` while a group runs, and removes the line on a
    normal reap;
  * ``run_reaper.reap()`` SIGTERMs (then SIGKILLs) every recorded group whose
    leader is still alive and was not started after the record — the start-time
    guard is what keeps a recycled pid safe.

Plus the retry-hint sanitiser: stream-JSON records are stripped from a detail
BEFORE truncation, so the failure line is not crowded out by thinking tokens.

Hermetic: temp dirs, a hand-rolled state.db, real tiny subprocess groups.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from mini_ork.dispatch.core import spawn_local
from mini_ork.orchestration import run_reaper
from mini_ork.recovery import retry_hint


_TASK_RUNS_DDL = """
CREATE TABLE task_runs (
  id                TEXT PRIMARY KEY,
  task_class        TEXT NOT NULL,
  recipe            TEXT,
  workflow_version  TEXT NOT NULL DEFAULT 'latest',
  kickoff_path      TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'classified',
  verdict           TEXT,
  cost_usd          REAL NOT NULL DEFAULT 0,
  duration_ms       INTEGER NOT NULL DEFAULT 0,
  trace_id          TEXT,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL,
  ended_at          INTEGER,
  notes             TEXT
)
"""

_RUN_EVENTS_DDL = """
CREATE TABLE run_events (
  event_id         TEXT PRIMARY KEY,
  run_id           TEXT NOT NULL,
  parent_run_id    TEXT,
  event_type       TEXT NOT NULL,
  payload_json     TEXT NOT NULL DEFAULT '{}',
  created_at       INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  last_heartbeat_at INTEGER NULL
)
"""


def _dead_pid() -> int:
    """A pid that exited and was reaped — gone for good, not a zombie."""
    for _ in range(10):
        proc = subprocess.Popen(["/bin/sleep", "0"])
        proc.wait()
        try:
            os.kill(proc.pid, 0)
        except ProcessLookupError:
            return proc.pid
    raise AssertionError("could not obtain a provably dead pid")


def _make_home(tmp_path: Path, run_id: str, *, agent_pgids: str = "") -> tuple[Path, Path]:
    home = tmp_path / "home"
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    now = int(time.time())
    con = sqlite3.connect(str(home / "state.db"))
    try:
        con.execute(_TASK_RUNS_DDL)
        con.execute(_RUN_EVENTS_DDL)
        con.execute(
            "INSERT INTO task_runs (id, task_class, kickoff_path, status,"
            " created_at, updated_at, notes) VALUES (?, 'framework_edit', 'k.md', 'executing', ?, ?, '')",
            (run_id, now, now))
        con.commit()
    finally:
        con.close()
    (run_dir / ".pid").write_text(f"{_dead_pid()}\n", encoding="utf-8")
    if agent_pgids:
        (run_dir / ".agent-pgids").write_text(agent_pgids, encoding="utf-8")
    return home, run_dir


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.05)
    return not _alive(pid)


def _spawn_group(term_immune: bool = False, marker: Path | None = None) -> tuple[subprocess.Popen, int | None]:
    """A real detached group leader; optionally records a child sharing the group."""
    body = "trap '' TERM; " if term_immune else ""
    if marker is not None:
        body += f"sleep 30 & echo $! > {shlex.quote(str(marker))}; wait"
    else:
        body += "while :; do sleep 0.2; done"
    leader = subprocess.Popen(
        ["/bin/bash", "-c", body],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    assert os.getpgid(leader.pid) == leader.pid
    if marker is None:
        return leader, None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not marker.is_file():
        assert leader.poll() is None
        time.sleep(0.02)
    child_pid = int(marker.read_text(encoding="utf-8").strip())
    return leader, child_pid


def _notes(home: Path, run_id: str) -> str:
    con = sqlite3.connect(str(home / "state.db"))
    try:
        row = con.execute("SELECT notes FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        con.close()
    return str(row[0]) if row and row[0] else ""


def _cleanup(leader: subprocess.Popen, pgid: int) -> None:
    subprocess.run(["/bin/bash", "-c", f"kill -9 -{pgid} 2>/dev/null"], check=False)
    try:
        leader.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


# ── reaper: stop the recorded groups of a dead run ──────────────────────────

def test_reap_terminates_recording_group_and_reports_it(tmp_path: Path) -> None:
    """A recorded sleeping group of a dead run is terminated; the note says so."""
    marker = tmp_path / "child.pid"
    leader, child_pid = _spawn_group(marker=marker)
    pgid = leader.pid
    try:
        home, _ = _make_home(tmp_path, "run-kill", agent_pgids=f"{pgid} {time.time():.0f}\n")
        reaped = run_reaper.reap(home)
        assert len(reaped) == 1
        assert "stopped 1 agent group(s)" in (reaped[0].get("reason") or "")
        assert "stopped 1 agent group(s)" in _notes(home, "run-kill")

        try:
            leader.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pytest.fail("a recorded live agent group must be terminated by reap")
        assert child_pid is not None and _wait_gone(child_pid), \
            "the whole group must die, not just its leader"
        deadline = time.monotonic() + 5.0
        while _group_alive(pgid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _group_alive(pgid)
    finally:
        _cleanup(leader, pgid)


def test_reap_does_not_signal_a_group_that_started_after_the_record(tmp_path: Path) -> None:
    """A recycled pid (started after the recorded ts) is never signalled."""
    leader, _child = _spawn_group()
    pgid = leader.pid
    try:
        stale = time.time() - 3600
        home, _ = _make_home(
            tmp_path, "run-recycled",
            agent_pgids=f"999999998 {stale:.0f}\n{pgid} {stale:.0f}\n")
        reaped = run_reaper.reap(home)
        assert len(reaped) == 1, "the dead run is still reaped"
        assert leader.poll() is None, "a recycled-pid group must not be signalled"
        assert _group_alive(pgid)
        assert "stopped 1 agent group(s)" not in _notes(home, "run-recycled")
    finally:
        _cleanup(leader, pgid)


def test_reap_escalates_a_sigterm_immune_group(tmp_path: Path) -> None:
    """SIGTERM first, then SIGKILL after the grace window."""
    leader, _ = _spawn_group(term_immune=True)
    pgid = leader.pid
    try:
        home, _ = _make_home(tmp_path, "run-esc", agent_pgids=f"{pgid} {time.time():.0f}\n")
        assert len(run_reaper.reap(home)) == 1
        try:
            rc = leader.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pytest.fail("a SIGTERM-immune group must be escalated to SIGKILL")
        assert rc == -signal.SIGKILL, f"expected death by SIGKILL, got rc={rc}"
    finally:
        _cleanup(leader, pgid)


# ── dispatch: record the live group, drop it on normal reap ────────────────

def _spawn_in_thread(argv: list[str], env: dict[str, str]) -> dict:
    box: dict = {}

    def _run() -> None:
        box["result"] = spawn_local(argv, stdin="", timeout=60, env=env, cwd=None)

    box["thread"] = threading.Thread(target=_run, daemon=True)
    box["thread"].start()
    return box


def test_dispatch_records_the_live_group_then_clears_it(tmp_path: Path) -> None:
    run_dir = tmp_path / "rd"
    run_dir.mkdir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "MINI_ORK_RUN_DIR": str(run_dir)}
    box = _spawn_in_thread(["/bin/sleep", "6"], env)

    pgid = ts = None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            content = (run_dir / ".agent-pgids").read_text(encoding="utf-8")
        except FileNotFoundError:
            content = ""
        match = re.fullmatch(r"(\d+) (\d+)\n", content)
        if match:
            pgid, ts = int(match.group(1)), int(match.group(2))
            break
        time.sleep(0.05)

    assert pgid is not None and ts is not None, \
        ".agent-pgids must hold '<pgid> <unix start ts>' while the agent runs"
    assert abs(ts - time.time()) <= 120
    assert pgid != os.getpgrp()
    assert _group_alive(pgid)

    box["thread"].join(timeout=60)
    assert box["result"][0] == 0
    leftover = ""
    try:
        leftover = (run_dir / ".agent-pgids").read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    assert not leftover.strip(), "a normally reaped group's line must be removed"


def test_dispatch_without_run_dir_writes_no_pgid_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MINI_ORK_RUN_DIR", raising=False)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    rc, _out, _err = spawn_local(["/bin/sleep", "0.2"], stdin="", timeout=60, env=env, cwd=None)
    assert rc == 0, "bookkeeping must not break a spawn without a run dir"
    assert not (tmp_path / ".agent-pgids").exists()
    assert not (Path.cwd() / ".agent-pgids").exists()


# ── retry-hint detail sanitiser ─────────────────────────────────────────────

PLAIN = "plain failure line: lint reported E501 on core.py"
ERROR_JSON = '{"error": "E501 line too long (x > 88 characters)"}'
STREAM = '{"type": "thinking", "thinking": "' + ("streamed reasoning token " * 3) + '"}'


def _is_stream_line(line: str) -> bool:
    try:
        value = json.loads(line)
    except ValueError:
        return False
    return isinstance(value, dict) and "type" in value


def _has_stream(text: str) -> bool:
    return any(_is_stream_line(line) for line in text.splitlines())


def test_strip_stream_json_keeps_plain_and_non_type_json() -> None:
    out = retry_hint._strip_stream_json("\n".join([STREAM, PLAIN, ERROR_JSON]) + "\n")
    assert not _has_stream(out)
    assert PLAIN in out.splitlines()
    assert ERROR_JSON in out.splitlines()


def test_tail_log_strips_before_truncating(tmp_path: Path) -> None:
    long_stream = '{"type": "assistant", "message": "' + ("padded stream payload " * 6) + '"}'
    blob = "\n".join([long_stream] * 18 + [ERROR_JSON, PLAIN]) + "\n"
    (tmp_path / "execute.log").write_text(blob, encoding="utf-8")
    detail = retry_hint._tail_log(tmp_path, "")
    assert PLAIN in detail.splitlines(), "truncation must not eat the plain failure line"
    assert ERROR_JSON in detail.splitlines()
    assert not _has_stream(detail)


def test_case_code_detail_has_no_stream_json() -> None:
    reason = "\n".join([PLAIN, ERROR_JSON] + [STREAM] * 3) + "\n"
    hint = retry_hint._case_code("test_verifier", {"status": "REFUTED", "reason": reason}, None, "run-x")
    assert hint is not None
    detail = hint["needs_change"]["detail"]
    assert PLAIN in detail.splitlines()
    assert not _has_stream(detail)
