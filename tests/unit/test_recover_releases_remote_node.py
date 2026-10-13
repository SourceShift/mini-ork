"""A recover of a ``--placement remote`` run releases the node it used.

The run's own lifecycle releases its node when it ends; an auto-repair recover
that runs afterwards re-opens a session, so the provider creates a fresh node
that nothing released before the TTL (Daytona e2e run-1791912327-41843)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))

from mini_ork.recovery import planner as rp  # noqa: E402
from test_recover_task_class import _setup  # noqa: E402


def test_dispatched_recover_releases_after_every_check(tmp_path, monkeypatch):
    run_id, db, workflow = _setup(tmp_path, monkeypatch)
    events: list[str] = []
    monkeypatch.setattr(rp, "_rerun_rubric_prescreen", lambda handoff: events.append("rubric"))
    monkeypatch.setattr("mini_ork.cli.main._release_remote_session",
                        lambda: events.append("release"))

    rc = rp.cli_main([run_id, "--workflow", workflow, "--db", db],
                     execute_fn=lambda argv: events.append("execute") or 0)

    assert rc == 0
    assert events == ["execute", "rubric", "release"]


def test_release_failure_never_changes_the_recover_rc(tmp_path, monkeypatch):
    run_id, db, workflow = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(rp, "_rerun_rubric_prescreen", lambda handoff: None)

    def boom():
        raise RuntimeError("node unreachable")

    monkeypatch.setattr("mini_ork.cli.main._release_remote_session", boom)
    assert rp.cli_main([run_id, "--workflow", workflow, "--db", db],
                       execute_fn=lambda argv: 0) == 0
