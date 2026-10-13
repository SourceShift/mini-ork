"""End-to-end ``mini-ork run --resume``: the executor writes the P1.2 ledger,
and a resume re-dispatches exactly the steps its plan line names.

``test_resume_cli.py`` pins the plan line against a ledger the test writes by
hand, and asserts the ``_run_handler`` wiring at the registry table only. That
left both ends unproven: nothing in the executor wrote the ledger, and the
handler fed a resume back into the full lifecycle, which classified, planned
and dispatched every node again. These tests drive the real executor instead,
with a stubbed model seam.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork import checkpoints as ck
from mini_ork.cli import execute as mex
from mini_ork.cli import main as mini_ork_main
from mini_ork.cli import run_resume
from mini_ork.stores import migrate as mc_migrate

RUN_ID = "run-resume-e2e"
RECIPE = "framework-edit"
TASK_CLASS = "framework_edit"

# Every variable the executor or the resume hand-off publishes. Registering
# them with monkeypatch up front restores them at teardown even though the
# code under test writes os.environ directly.
_PUBLISHED_ENV = (
    "MINI_ORK_RUN_ID", "MINI_ORK_TASK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_DB",
    "MINI_ORK_WORKFLOW", "MINI_ORK_RECIPE", "MINI_ORK_TASK_CLASS",
    "MINI_ORK_RECOVERY_RUN_ID", "MINI_ORK_RECOVERY_CLOSURE",
    "MINI_ORK_RECOVERY_STRATEGY", "MINI_ORK_RECOVERY_FROM",
    "MINI_ORK_RECOVERY_ACTIVE", "MINI_ORK_RECOVERY_SKU", "MINI_ORK_PLAN_PATH",
    "MO_NODE_ID",
)


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, check=True)


@pytest.fixture
def run(tmp_path: Path, monkeypatch) -> dict:
    """A migrated home holding one run of a linear A → B → C workflow.

    The nodes are framework-edit implementers, so each one that succeeds must
    really change the target tree (the ground-truth harvest fails a no-op).
    """
    for name in _PUBLISHED_ENV:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    run_dir = home / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    db = home / "state.db"
    rc, _out, _err = mc_migrate.init_db(db=str(db), root=str(REPO))
    assert rc == 0
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO task_runs (id, task_class, recipe, kickoff_path, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 'executing', 1000000, 1000000)",
        (RUN_ID, TASK_CLASS, RECIPE, str(tmp_path / "kickoff.md")),
    )
    con.commit()
    con.close()

    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(yaml.safe_dump({
        "version": "0.1.0", "task_class": TASK_CLASS,
        "nodes": [{"name": n, "type": "implementer", "model_lane": "minimax_lens"}
                  for n in ("A", "B", "C")],
        "edges": [{"from": "A", "to": "B", "edge_type": "depends_on"},
                  {"from": "B", "to": "C", "edge_type": "depends_on"}],
    }, sort_keys=False))
    (run_dir / "plan.json").write_text(json.dumps({"objective": "resume e2e"}))

    target = tmp_path / "target"
    target.mkdir()
    _git(target, "init", "-q")
    _git(target, "config", "user.email", "t@t")
    _git(target, "config", "user.name", "t")
    (target / "seed.txt").write_text("seed\n")
    _git(target, "add", "seed.txt")
    _git(target, "commit", "-qm", "base")

    for name, value in {
        "MINI_ORK_HOME": str(home), "MINI_ORK_DB": str(db),
        "MINI_ORK_RUN_ID": RUN_ID, "MINI_ORK_RUN_DIR": str(run_dir),
        "MINI_ORK_WORKFLOW": str(workflow), "MINI_ORK_RECIPE": RECIPE,
        "MINI_ORK_TASK_CLASS": TASK_CLASS, "MO_TARGET_CWD": str(target),
        "MO_AUTO_REPAIR": "0",
    }.items():
        monkeypatch.setenv(name, value)
    ck.enable(True)
    return {"db": str(db), "run_dir": run_dir, "target": target}


def _model(seen: list[str], target: Path, *, fail: frozenset[str] = frozenset()):
    """A stub model seam: records the node, edits the tree unless it fails."""
    def dispatch(task_class, node_type, prompt):
        del task_class, node_type, prompt
        node = os.environ.get("MO_NODE_ID", "?")
        seen.append(node)
        if node in fail:
            return 1, f"stub failure for {node}"
        (target / f"{node}-{len(seen)}.txt").write_text(f"edit by {node}\n")
        return 0, f"stub output for {node}"
    return dispatch


def _ledger(db: str) -> dict[str, str]:
    return {r.step_id: r.status for r in ck.list_completions(db, run_id=RUN_ID)}


def test_executor_writes_the_ledger_and_resume_skips_what_succeeded(run, capsys):
    first: list[str] = []
    rc = mex.main([], dispatch_fn=_model(first, run["target"], fail=frozenset({"B"})))
    assert rc != 0
    assert first == ["A", "B"]
    # C never ran (its parent failed), so it has no row — a resume must run it.
    assert _ledger(run["db"]) == {"A": "succeeded", "B": "failed"}
    capsys.readouterr()

    rc, sink = run_resume.run_with_resume(["--resume", RUN_ID])
    assert rc == run_resume.RC_OK
    plan_line = capsys.readouterr().out
    assert "skip=[A]" in plan_line
    assert "redispatch=[B,C]" in plan_line

    second: list[str] = []
    rc = run_resume.dispatch_resume(
        sink, root=str(REPO),
        execute_fn=lambda argv: mex.main(argv, dispatch_fn=_model(second, run["target"])),
    )
    assert rc == 0
    # The property the plan line promises: A is not executed again.
    assert second == ["B", "C"]
    assert _ledger(run["db"]) == {"A": "succeeded", "B": "succeeded", "C": "succeeded"}


def test_a_run_without_checkpoints_is_refused_not_started_over(run, capsys):
    rc, sink = run_resume.run_with_resume(["--resume", RUN_ID])
    assert rc == run_resume.RC_OK

    def must_not_execute(argv):
        raise AssertionError(f"resume executed {argv} for a run with no checkpoints")

    rc = run_resume.dispatch_resume(sink, root=str(REPO), execute_fn=must_not_execute)
    assert rc == run_resume.RC_UNKNOWN_RUN
    assert "no completion checkpoints" in capsys.readouterr().err


def test_a_recipe_argument_naming_another_recipe_is_refused(run, capsys):
    ck.record_completion(run["db"], run_id=RUN_ID, step_id="A", node_id="A",
                         status="succeeded", rc=0, started_at=0, ended_at=1)
    rc, sink = run_resume.run_with_resume(["code-fix", "--resume", RUN_ID])
    assert rc == run_resume.RC_OK

    def must_not_execute(argv):
        raise AssertionError("resume executed under the wrong recipe")

    rc = run_resume.dispatch_resume(sink, root=str(REPO), execute_fn=must_not_execute)
    assert rc == run_resume.RC_UNKNOWN_RUN
    assert "ran recipe 'framework-edit', not 'code-fix'" in capsys.readouterr().err


def test_checkpoint_id_rewinds_by_completion_order_not_by_name(run):
    # Recorded in this order; alphabetically "alpha" < "mid" < "zeta".
    for step in ("zeta", "alpha", "mid"):
        ck.record_completion(run["db"], run_id=RUN_ID, step_id=step, node_id=step,
                             status="succeeded", rc=0, started_at=0, ended_at=1)
    plan_line, dispatch = run_resume.build_resume_plan(
        run["db"], RUN_ID, candidate_steps=["zeta", "alpha", "mid"],
        checkpoint_id="alpha",
    )
    assert "skip=[zeta]" in plan_line
    assert dispatch == ["alpha", "mid"]


def test_an_unknown_checkpoint_id_is_refused_without_binding_the_run(run, monkeypatch, capsys):
    ck.record_completion(run["db"], run_id=RUN_ID, step_id="A", node_id="A",
                         status="succeeded", rc=0, started_at=0, ended_at=1)
    monkeypatch.delenv("MINI_ORK_RUN_ID")
    rc, sink = run_resume.run_with_resume(
        ["--resume", RUN_ID, "--checkpoint-id", "no-such-step"])
    assert rc == run_resume.RC_UNKNOWN_RUN
    assert "unknown checkpoint-id 'no-such-step'" in capsys.readouterr().err
    assert "MINI_ORK_RUN_ID" not in os.environ
    assert "resume_dispatch" not in sink


def test_run_handler_sends_a_resume_to_the_ledger_never_to_the_lifecycle(run, monkeypatch):
    ck.record_completion(run["db"], run_id=RUN_ID, step_id="A", node_id="A",
                         status="succeeded", rc=0, started_at=0, ended_at=1)
    dispatched: list[dict] = []

    def lifecycle(rest, root):
        raise AssertionError("a resume re-entered the full lifecycle")

    monkeypatch.setattr(mini_ork_main, "_run_lifecycle", lifecycle)
    monkeypatch.setattr(run_resume, "dispatch_resume",
                        lambda sink, *, root: dispatched.append(sink) or 0)
    assert mini_ork_main._run_handler(["--resume", RUN_ID], str(REPO)) == 0
    assert [sink["resume_dispatch"] for sink in dispatched] == [["B", "C"]]
