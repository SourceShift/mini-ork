"""Unit tests: the ``rollback`` node handler's target resolution.

``_handle_rollback`` used to roll back ``("workflow", recipe)`` and then
``("agent", "default")``. No live row is named ``"default"`` — an applied prompt
mutation is named by its absolute target path — so the agent half was a
guaranteed no-op that still set ``reverted = True`` and printed success. The
handler now resolves the rows this run actually promoted from the implementer's
recorded ``files_changed``.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute_handlers as ex
from mini_ork.cli.execute import NodeDispatch
from mini_ork.registries import version_registry as vr

_NOOP = lambda *_a, **_k: None  # noqa: E731 - NodeDispatch seam stub


def _rows(db: str) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM version_registry")]
    finally:
        con.close()


def _ctx(db: str, root: Path, run_dir: Path, recipe: str = "code-fix") -> NodeDispatch:
    return NodeDispatch(
        node_id="rollback", node_type="rollback", node_desc="undo the run",
        prompt_ref="", verifier_ref="", model_lane="reviewer",
        node_requires_capabilities="", root=str(root), run_dir=str(run_dir),
        plan_path="", task_class="code-fix", db=db, run_id="run-rb-1",
        recipe=recipe, workflow="", lane="reviewer",
        run_dir_eff=str(run_dir), recipe_dir="", prompt_file="",
        plan_content="", learned="",
        dispatch_fn=_NOOP, trace=_NOOP, charge=_NOOP,
    )


def _record_changes(run_dir: Path, files: list[str]) -> None:
    (run_dir / "implementer-summary.json").write_text(
        json.dumps({"files_changed": files}), encoding="utf-8")


def _promote(db: str, target: Path, before: str, after: str) -> str:
    return vr.register("agent", json.dumps({
        "name": str(target), "version_id": None, "status": "stable",
        "target_path": str(target), "content": after,
        "baseline_content": before}), db=db)


def test_handle_rollback_reverts_the_runs_promoted_target(
        tmp_path, monkeypatch):
    """The run's promoted prompt is restored by name, not by "default".

    The registry row for an applied mutation is named by its absolute target
    path, so the old hardcoded ``("agent", "default")`` could never match it.
    """
    db = str(tmp_path / "state.db")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = tmp_path / "reviewer.md"
    target.write_text("AFTER\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    _record_changes(run_dir, [str(target)])
    _promote(db, target, "BEFORE\n", "AFTER\n")

    rc, finish = ex._handle_rollback(_ctx(db, tmp_path, run_dir))

    assert (rc, finish) == (0, "done")
    assert target.read_text(encoding="utf-8") == "BEFORE\n"
    rows = {r["version_id"]: r for r in _rows(db)}
    retired = [r for r in rows.values() if r["status"] == "retired"]
    assert len(retired) == 1


def test_handle_rollback_leaves_untouched_targets_alone(tmp_path, monkeypatch):
    """Only rows matching the run's changed files are rolled back.

    A second promoted prompt in the same DB — from another run — must survive.
    """
    db = str(tmp_path / "state.db")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    mine = tmp_path / "reviewer.md"
    theirs = tmp_path / "planner.md"
    mine.write_text("AFTER\n", encoding="utf-8")
    theirs.write_text("THEIRS AFTER\n", encoding="utf-8")
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    _record_changes(run_dir, [str(mine)])
    _promote(db, mine, "BEFORE\n", "AFTER\n")
    _promote(db, theirs, "THEIRS BEFORE\n", "THEIRS AFTER\n")

    ex._handle_rollback(_ctx(db, tmp_path, run_dir))

    assert mine.read_text(encoding="utf-8") == "BEFORE\n"
    assert theirs.read_text(encoding="utf-8") == "THEIRS AFTER\n"


def test_handle_rollback_reports_when_there_is_nothing_to_revert(
        tmp_path, monkeypatch, capsys):
    """No recorded changes and no workflow row: a report, not a false success.

    The node still succeeds (rc 0) — it never re-fails an already-failed run —
    but it must say so rather than claim a revert that did not happen.
    """
    db = str(tmp_path / "state.db")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))

    rc, finish = ex._handle_rollback(_ctx(db, tmp_path, run_dir, recipe=""))

    assert (rc, finish) == (0, "done")
    assert "nothing to revert" in capsys.readouterr().err
