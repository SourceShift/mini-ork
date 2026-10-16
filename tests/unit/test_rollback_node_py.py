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

import pytest

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


def _workflow(tmp_path: Path, strategy: str) -> str:
    path = tmp_path / "workflow.yaml"
    path.write_text(f"rollback_strategy: {strategy}\n", encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("strategy,recipe", [
    ("keep_partial_drain_discard_failed_iter", "recursive-validate-impl"),
    ("keep_partial_pass_discard_failed_iter", "spec-driven-delivery"),
    ("keep_partial_pass_discard_failed_iter", "doc-to-features-loop"),
])
def test_a_declared_keep_partial_strategy_keeps_the_tree_and_says_so(
        tmp_path, monkeypatch, capsys, strategy, recipe):
    """Every declared ``keep_partial_*`` strategy must name itself in the log.

    ``keep_partial`` means the failed iteration's edit stays in the target tree on
    purpose — the run re-enters from the failing node, so discarding the edit
    would force a re-implement. Three recipes declare a variant
    (``recursive-validate-impl``: drain; ``spec-driven-delivery`` and
    ``doc-to-features-loop``: pass) and the handler had a branch for only the
    first: the other two fell through to the bare "[ok] rollback complete" line,
    indistinguishable in the log from a revert that had run — and were in fact
    reported by the generic ``unhandled rollback_strategy`` warning. The work was
    untouched in every case (run-1791821930-72687: the implementer's commit was
    still at HEAD afterwards and ``git reflog`` shows no reset;
    run-1792151796-92942 hit the warning path on spec-driven-delivery). Name the
    strategy so the log cannot be misread again, and keep the tree.
    """
    db = str(tmp_path / "state.db")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))
    work = tmp_path / "impl.ts"
    work.write_text("KEPT\n", encoding="utf-8")
    _record_changes(run_dir, [str(work)])

    ctx = _ctx(db, tmp_path, run_dir, recipe=recipe)
    ctx.workflow = _workflow(tmp_path, strategy)
    rc, finish = ex._handle_rollback(ctx)

    assert (rc, finish) == (0, "done")
    err = capsys.readouterr().err
    assert strategy in err
    assert "unhandled rollback_strategy" not in err
    assert work.read_text(encoding="utf-8") == "KEPT\n"


def test_an_unhandled_strategy_warns_instead_of_going_quiet(
        tmp_path, monkeypatch, capsys):
    """A strategy nobody implements must not silently no-op."""
    db = str(tmp_path / "state.db")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setenv("MINI_ORK_ROOT", str(tmp_path))

    ctx = _ctx(db, tmp_path, run_dir)
    ctx.workflow = _workflow(tmp_path, "some_future_strategy")
    ex._handle_rollback(ctx)

    assert "unhandled rollback_strategy 'some_future_strategy'" in capsys.readouterr().err
