"""RBF-07 (+ RB-45): the reaper must reap a remote-placement run whose local
dispatcher died, free its remote nodes, and render notes in UTC.

Hermetic: a tmp home with its own sqlite (built by the runtime's migration
runner), a throwaway ``python -c pass`` child for a genuinely dead pid, and
monkeypatched provider release functions — no network, no provider
credentials, no writes outside tmp.
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

import mini_ork
import mini_ork.orchestration.run_reaper as run_reaper
import mini_ork.remote.providers.base as provider_base
import mini_ork.remote.providers.run_nodes as provider_run_nodes
from mini_ork.stores import migrate as mig

REPO = Path(mini_ork.__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    (h / "runs").mkdir()
    return h


def _seed(home: Path, run_id: str, status: str = "executing") -> Path:
    now = int(time.time())
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, task_class, kickoff_path, status, created_at, updated_at)"
        " VALUES (?, 'framework_edit', 'kickoff.md', ?, ?, ?)",
        (run_id, status, now, now))
    con.commit()
    con.close()
    run_dir = home / "runs" / run_id
    run_dir.mkdir(parents=True)
    return run_dir


def _row(home: Path, run_id: str) -> tuple[str, str | None]:
    con = sqlite3.connect(home / "state.db")
    try:
        row = con.execute("SELECT status, notes FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        return (row[0], row[1]) if row else ("", None)
    finally:
        con.close()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _write_pid(run_dir: Path, pid: int) -> None:
    (run_dir / ".pid").write_text(f"{pid}\n", encoding="utf-8")


def _write_marker(run_dir: Path) -> None:
    (run_dir / ".remote-sync-state.json").write_text('{"state": "synced"}\n', encoding="utf-8")


def _write_node_record(run_dir: Path, run_id: str) -> None:
    (run_dir / ".node-provider.json").write_text(
        json.dumps({"provider": "daytona", "handle": "node-h1", "run_id": run_id}) + "\n",
        encoding="utf-8")


def _spy(result=None, exc=None, on_call=None):
    calls: list[Path] = []

    def fn(run_dir, env):
        calls.append(Path(run_dir))
        if on_call is not None:
            on_call()
        if exc is not None:
            raise exc
        return result

    fn.calls = calls
    return fn


# ── probe: the .pid decides when it names a process ────────────────────────

def test_dead_pid_beats_the_remote_marker(home: Path) -> None:
    run_dir = _seed(home, "run-remote-dead")
    _write_marker(run_dir)
    _write_pid(run_dir, _dead_pid())

    assert run_reaper.probe(run_dir).verdict == "dead"


def test_alive_pid_with_the_marker_is_alive(home: Path) -> None:
    run_dir = _seed(home, "run-remote-alive")
    _write_marker(run_dir)
    _write_pid(run_dir, os.getpid())

    assert run_reaper.probe(run_dir).verdict == "alive"


def test_marker_without_a_pid_stays_remote_and_is_not_reaped(home: Path) -> None:
    run_dir = _seed(home, "run-mirrored")
    _write_marker(run_dir)

    assert run_reaper.probe(run_dir).verdict == "remote"
    assert run_reaper.reap(home) == []
    assert _row(home, "run-mirrored")[0] == "executing"


def test_a_cost_pause_still_precedes_the_pid_check(home: Path) -> None:
    run_dir = _seed(home, "run-paused")
    _write_marker(run_dir)
    _write_pid(run_dir, os.getpid())
    (run_dir / ".cost-pause").write_text("", encoding="utf-8")

    assert run_reaper.probe(run_dir).verdict == "paused"


# ── reap: fail the row, then free its nodes ────────────────────────────────

def test_reap_fails_a_dead_remote_marked_run(home: Path) -> None:
    run_dir = _seed(home, "run-remote-reaped")
    _write_marker(run_dir)
    _write_pid(run_dir, _dead_pid())

    entries = run_reaper.reap(home)

    assert [e["run_id"] for e in entries] == ["run-remote-reaped"]
    status, notes = _row(home, "run-remote-reaped")
    assert status == "failed"
    assert notes is not None and "reaped: dispatcher" in notes
    assert not (run_dir / ".pid").exists()


def test_reap_releases_both_node_levels_after_the_failure(home: Path,
                                                         monkeypatch) -> None:
    run_id = "run-remote-nodes"
    run_dir = _seed(home, run_id)
    _write_marker(run_dir)
    _write_pid(run_dir, _dead_pid())
    _write_node_record(run_dir, run_id)
    statuses: list[str] = []
    run_node = _spy(result=True, on_call=lambda: statuses.append(_row(home, run_id)[0]))
    all_nodes = _spy(result=2)
    monkeypatch.setattr(provider_base, "release_run_node", run_node)
    monkeypatch.setattr(provider_run_nodes, "release_all_nodes", all_nodes)

    entries = run_reaper.reap(home)

    assert [e["run_id"] for e in entries] == [run_id]
    assert statuses == ["failed"], "nodes may be freed only after the row is failed"
    assert run_node.calls == [run_dir]
    assert all_nodes.calls == [run_dir]
    _status, notes = _row(home, run_id)
    # 1 run-level node + 2 per-node sandboxes.
    assert notes is not None and "; released 3 node(s)" in notes
    assert entries[0]["reason"].endswith("; released 3 node(s)")


def test_reap_gates_the_run_level_release_on_the_node_record(home: Path,
                                                            monkeypatch) -> None:
    run_id = "run-no-run-node"
    run_dir = _seed(home, run_id)
    _write_marker(run_dir)
    _write_pid(run_dir, _dead_pid())
    run_node = _spy(result=True)
    all_nodes = _spy(result=0)
    monkeypatch.setattr(provider_base, "release_run_node", run_node)
    monkeypatch.setattr(provider_run_nodes, "release_all_nodes", all_nodes)

    assert [e["run_id"] for e in run_reaper.reap(home)] == [run_id]

    assert run_node.calls == [], "no .node-provider.json → no run-level release"
    assert all_nodes.calls == [run_dir]
    _status, notes = _row(home, run_id)
    assert notes is not None and "; released 0 node(s)" in notes


def test_reap_survives_a_release_that_raises(home: Path, monkeypatch,
                                             capsys) -> None:
    run_id = "run-release-boom"
    run_dir = _seed(home, run_id)
    _write_marker(run_dir)
    _write_pid(run_dir, _dead_pid())
    _write_node_record(run_dir, run_id)
    run_node = _spy(exc=RuntimeError("daytona release blew up"))
    all_nodes = _spy(exc=RuntimeError("sandbox release blew up"))
    monkeypatch.setattr(provider_base, "release_run_node", run_node)
    monkeypatch.setattr(provider_run_nodes, "release_all_nodes", all_nodes)

    entries = run_reaper.reap(home)  # must not raise

    assert [e["run_id"] for e in entries] == [run_id]
    assert len(run_node.calls) == 1 and len(all_nodes.calls) == 1
    status, notes = _row(home, run_id)
    assert status == "failed"
    assert notes is not None and "reaped:" in notes
    assert capsys.readouterr().err.strip() != ""


def test_dry_run_releases_nothing(home: Path, monkeypatch) -> None:
    run_id = "run-dry"
    run_dir = _seed(home, run_id)
    _write_marker(run_dir)
    _write_pid(run_dir, _dead_pid())
    _write_node_record(run_dir, run_id)
    run_node = _spy(result=True)
    all_nodes = _spy(result=1)
    monkeypatch.setattr(provider_base, "release_run_node", run_node)
    monkeypatch.setattr(provider_run_nodes, "release_all_nodes", all_nodes)

    assert [e["run_id"] for e in run_reaper.reap(home, dry_run=True)] == [run_id]

    assert run_node.calls == [] and all_nodes.calls == []
    assert _row(home, run_id)[0] == "executing"
    assert (run_dir / ".pid").exists()


# ── notes are UTC ──────────────────────────────────────────────────────────

def test_iso_is_utc_with_a_z_suffix() -> None:
    had_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"  # a non-UTC zone proves UTC, not local
    time.tzset()
    try:
        assert run_reaper._iso(0) == "1970-01-01T00:00:00Z"
    finally:
        if had_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = had_tz
        time.tzset()
