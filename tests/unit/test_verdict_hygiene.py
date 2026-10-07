"""K0.5b — verdict hygiene.

AC1: the advisory rubric pre-screen records a score; it never approves a
commit, satisfies the panel approval gate, or replaces the run verdict.
AC2: a reviewer ``verdict=unknown`` fails the node explicitly with reason
``reviewer_verdict_unparseable`` (trace + task_runs notes + a [fail] line).

Decision (MO_REVISE_ROUNDS): an unparseable verdict does NOT spend a revise
round. There are no findings to send back, so re-running the implementer would
burn a full implementer+verify+review cycle on a reviewer format failure; the
run fails at once with the reason recorded and rollback proceeds as usual.
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
from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.cli import publisher  # noqa: E402
from mini_ork.orchestration import run_reaper  # noqa: E402

RUBRIC_FAIL = {"panel_score": 12.5, "pass": False, "source": "rubric-prescreen",
               "task_class": "framework_edit", "scale": "rubric 0-8 mapped to 0-100"}
RUBRIC_PASS = {**RUBRIC_FAIL, "panel_score": 87.5, "pass": True}


@pytest.fixture(autouse=True)
def _isolate_run_environment(monkeypatch):
    for name in ("MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_PLAN_PATH", "MINI_ORK_RUN_ID",
                 "MO_TARGET_CWD", "MO_APPLY_IMPL_OUTPUT", "MINI_ORK_WORKFLOW", "MINI_ORK_RECIPE_ROOT",
                 "MO_REVISE_ROUNDS", "MO_GRADE_RUN_REWARD", "MO_LEARNING_WRITEBACK", "MO_LANE_ROUTER"):
        monkeypatch.delenv(name, raising=False)


# ── AC2 — driven through the real executor (fake LLM seam) ──────────────────

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
        _git(repo, *args)
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "base")
    return repo


def _seed_db(tmp_path: Path) -> str:
    home = tmp_path / "db" / ".mini-ork"
    home.mkdir(parents=True)
    db = str(home / "state.db")
    subprocess.run(["bash", str(REPO / "db" / "init.sh")],
                   env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
                   capture_output=True, text=True, check=True)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,cost_usd,"
                "created_at,updated_at) VALUES ('r','code_fix','v1','k.md','planned',0,"
                "strftime('%s','now','-60 seconds'),strftime('%s','now'))")
    con.commit()
    con.close()
    return db


def _workflow() -> dict:
    return {
        "version": "0.1.0", "task_class": "code_fix", "dispatch_mode": "parallel",
        "nodes": [
            {"name": "planner", "type": "planner", "dispatch_mode": "serial"},
            {"name": "implementer", "type": "implementer", "model_lane": "worker", "dispatch_mode": "serial"},
            {"name": "reviewer", "type": "reviewer", "model_lane": "reviewer", "dispatch_mode": "serial"},
            {"name": "rollback", "type": "rollback", "dispatch_mode": "serial"},
        ],
        "edges": [
            {"from": "planner", "to": "implementer", "edge_type": "depends_on"},
            {"from": "implementer", "to": "reviewer", "edge_type": "depends_on"},
            {"from": "reviewer", "to": "rollback", "edge_type": "escalates_to"},
            {"from": "reviewer", "to": "implementer", "edge_type": "retries", "max_rounds": 2},
        ],
    }


def _run(tmp_path, monkeypatch, reviewer_answer: str):
    recipe_dir = tmp_path / "overlay" / "recipes" / "hygiene-test"
    recipe_dir.mkdir(parents=True)
    (recipe_dir / "workflow.yaml").write_text(yaml.safe_dump(_workflow()), encoding="utf-8")
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plan = run_dir / "plan.json"
    plan.write_text(json.dumps({"objective": "o", "task_class": "code_fix"}))
    db = _seed_db(tmp_path)
    for name, value in {"MO_TARGET_CWD": str(repo), "MINI_ORK_WORKFLOW": str(recipe_dir / "workflow.yaml"),
                        "MINI_ORK_PLAN_PATH": str(plan), "MINI_ORK_RUN_DIR": str(run_dir),
                        "MINI_ORK_RUN_ID": "r", "MINI_ORK_DB": db,
                        "MINI_ORK_HOME": str(tmp_path / "db" / ".mini-ork"),
                        "MINI_ORK_RECIPE_ROOT": str(tmp_path / "overlay"), "MINI_ORK_RECIPE": "hygiene-test",
                        "MO_GRADE_RUN_REWARD": "0", "MO_LEARNING_WRITEBACK": "0", "MO_LANE_ROUTER": "0"}.items():
        monkeypatch.setenv(name, value)
    calls = {"implementer": 0, "reviewer": 0}

    def fake_dispatch(task_class, lane, prompt):
        if "Implement:" in prompt:
            calls["implementer"] += 1
            (repo / "a.py").write_text(f"x = {calls['implementer'] + 1}\n")
            return 0, "done"
        if "Review the implementation" in prompt:
            calls["reviewer"] += 1
            return 0, reviewer_answer
        return 0, "done"

    rc = ex.main(argv=[], root=str(REPO), dispatch_fn=fake_dispatch)
    return rc, calls, db


def test_unparseable_verdict_fails_explicitly_and_spends_no_revise_round(tmp_path, monkeypatch, capsys):
    rc, calls, db = _run(tmp_path, monkeypatch,
                         "Looks broadly fine to me, but I am not sure about the edge cases.")
    assert rc == 1
    assert calls == {"implementer": 1, "reviewer": 1}  # no revise round
    err = capsys.readouterr().err
    assert "reviewer_verdict_unparseable" in err and "[revise] skipped for reviewer" in err
    con = sqlite3.connect(db)
    traced = [r[0] for r in con.execute(
        "SELECT reviewer_verdict FROM execution_traces WHERE run_id='r' AND reviewer_verdict IS NOT NULL")]
    notes = con.execute("SELECT notes FROM task_runs WHERE id='r'").fetchone()[0] or ""
    con.close()
    assert "reviewer_verdict_unparseable" in traced
    assert "reviewer_verdict_unparseable: node reviewer" in notes


def test_a_real_rejection_still_gets_its_revise_round(tmp_path, monkeypatch):
    rc, calls, _db = _run(tmp_path, monkeypatch,
                          json.dumps({"verdict": "needs_revision", "notes": ["missing test"]}))
    assert rc == 1 and calls["implementer"] == 3  # 1 + MO_REVISE_ROUNDS (2)


# ── AC1 — the rubric score is never a verdict ────────────────────────────────

def _w(path: Path, obj) -> Path:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def test_is_rubric_prescreen_tells_a_score_from_a_panel_verdict(tmp_path: Path) -> None:
    assert publisher.is_rubric_prescreen(_w(tmp_path / "a.json", RUBRIC_PASS))
    assert not publisher.is_rubric_prescreen(_w(tmp_path / "b.json", {"verdict": "APPROVE"}))
    assert not publisher.is_rubric_prescreen(tmp_path / "missing.json")


def test_a_passing_rubric_does_not_approve_a_commit_the_reviewer_rejected(tmp_path, capsys) -> None:
    _w(tmp_path / "panel-verdict.json", RUBRIC_PASS)
    _w(tmp_path / "review-verdict.json", {"verdict": "needs_revision"})
    assert publisher._publisher_try_commit_files(str(REPO), str(tmp_path), str(tmp_path), "", "",
                                                 "code-fix", "implementer", "r") is False
    assert "resolved: 'needs_revision'" in capsys.readouterr().err


def test_a_rubric_file_never_satisfies_the_panel_approval_gate(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MO_ORACLE_GATES_AUTO", "0")
    monkeypatch.setenv("MO_LEVEL_VECTOR", "0")
    _w(tmp_path / "panel-verdict.json", RUBRIC_PASS)
    rc, reason = publisher.publisher_node(str(REPO), str(tmp_path), "", "r", "recursive-validate-impl",
                                          "recursive_validate_impl")
    assert (rc, reason) == (1, "verdict_fail")


def test_a_rubric_file_does_not_suppress_the_run_verdict(tmp_path: Path) -> None:
    _w(tmp_path / "panel-verdict.json", RUBRIC_FAIL)
    ex._emit_run_verdict(str(tmp_path), 0, 3)
    assert json.loads((tmp_path / "verdict.json").read_text())["verdict"] == "pass"


def test_a_real_panel_verdict_still_owns_the_run_verdict(tmp_path: Path) -> None:
    _w(tmp_path / "panel-verdict.json", {"verdict": "APPROVE", "iteration": 1})
    ex._emit_run_verdict(str(tmp_path), 0, 3)
    assert not (tmp_path / "verdict.json").exists()


def test_a_failing_rubric_does_not_change_a_passed_runs_final_status(tmp_path: Path) -> None:
    from mini_ork.stores import migrate as mig

    home = tmp_path / ".mini-ork"
    home.mkdir()
    rc, _o, err = mig.init_db(db=str(home / "state.db"), root=str(REPO))
    assert rc == 0, err
    con = sqlite3.connect(home / "state.db")
    con.execute("INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,cost_usd,created_at,"
                "updated_at) VALUES ('p','code_fix','v1','k','executing',0,1,1)")
    con.commit()
    con.close()
    run_dir = home / "runs" / "p"
    run_dir.mkdir(parents=True)
    _w(run_dir / "verdict.json", {"verdict": "pass", "failed_nodes": 0, "source": "execute@run-level"})
    _w(run_dir / "panel-verdict.json", RUBRIC_FAIL)
    run_reaper.close_run_record(home / "state.db", "p", run_dir, crashed=False, rc=0)
    assert sqlite3.connect(home / "state.db").execute("SELECT status FROM task_runs WHERE id='p'").fetchone()[0] \
        == "published"
