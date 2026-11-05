"""The run → orchestrator-thread binding the agent writes and the pages read."""
from __future__ import annotations

import json
from pathlib import Path

from mini_ork import orchestrator_thread


def test_a_written_binding_reads_back(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    assert orchestrator_thread.write(run_dir, "orch-1-ab") is True
    assert orchestrator_thread.read(run_dir) == "orch-1-ab"
    payload = json.loads(
        (run_dir / orchestrator_thread.FILENAME).read_text(encoding="utf-8"))
    assert payload["session_id"] == "orch-1-ab"


def test_a_missing_run_directory_is_not_created(tmp_path: Path) -> None:
    """A follow also starts for run ids with no directory (a pruned run, a test
    harness). The binding describes a run — it must not grow one."""
    run_dir = tmp_path / "runs" / "run-absent"
    assert orchestrator_thread.write(run_dir, "orch-1-ab") is False
    assert not run_dir.exists()


def test_an_empty_session_id_is_not_a_binding(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    assert orchestrator_thread.write(run_dir, "  ") is False
    assert orchestrator_thread.read(run_dir) is None


def test_owner_json_naming_a_thread_is_the_fallback(tmp_path: Path) -> None:
    """A run launched from a thread carries ``MO_RUN_OWNER=thread:<session>``
    and may be bound before this file existed."""
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "owner.json").write_text(
        json.dumps({"kind": "thread", "id": "orch-2-cd", "label": "thread:orch-2-cd"}),
        encoding="utf-8")
    assert orchestrator_thread.read(run_dir) == "orch-2-cd"


def test_a_user_owner_is_not_an_orchestrator(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "owner.json").write_text(
        json.dumps({"kind": "user", "id": "a@b.c", "label": "user:a@b.c"}),
        encoding="utf-8")
    assert orchestrator_thread.read(run_dir) is None


def test_a_corrupt_record_reads_as_unbound(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "orchestrator.json").write_text("{not json", encoding="utf-8")
    assert orchestrator_thread.read(run_dir) is None


def test_run_dir_is_the_direct_run_directory(tmp_path: Path) -> None:
    direct = tmp_path / "runs" / "run-1"
    direct.mkdir(parents=True)
    assert orchestrator_thread.run_dir(tmp_path, "run-1") == direct


def test_run_dir_finds_an_rsi_child_run(tmp_path: Path) -> None:
    """An rsi cycle stages its child runs in their own nested home while the
    IDE lists them from the parent ``state.db``."""
    nested = tmp_path / "rsi" / "cand-1" / "rsi-cycle" / "home" / "runs" / "run-9"
    nested.mkdir(parents=True)
    assert orchestrator_thread.run_dir(tmp_path, "run-9") == nested
    assert orchestrator_thread.write(nested, "orch-9-xy") is True
    assert orchestrator_thread.read_run(tmp_path, "run-9") == "orch-9-xy"


def test_run_dir_finds_a_child_home_run(tmp_path: Path) -> None:
    nested = tmp_path / "rsi" / "cand-1" / "child-home" / "runs" / "run-8"
    nested.mkdir(parents=True)
    assert orchestrator_thread.run_dir(tmp_path, "run-8") == nested


def test_run_dir_falls_back_to_the_direct_path(tmp_path: Path) -> None:
    """A run that is not staged yet keeps the path a later retry re-resolves."""
    assert orchestrator_thread.run_dir(tmp_path, "run-late") == tmp_path / "runs" / "run-late"
    assert orchestrator_thread.read_run(tmp_path, "run-late") is None


def test_run_dir_does_not_glob_outside_the_tree(tmp_path: Path) -> None:
    assert orchestrator_thread.run_dir(tmp_path, "../escape") == tmp_path / "runs" / "../escape"
