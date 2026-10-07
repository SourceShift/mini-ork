"""The revise loop: a failed ``retries``-edge node sends its findings back to the
implementer for a bounded number of rounds, instead of blocking descendants and
throwing the work away.

Driven through ``ex.main(..., dispatch_fn=fake_dispatch)`` with a code-fix-shaped
workflow and a recipe overlay (``MINI_ORK_RECIPE_ROOT``) whose verifier scripts
are tiny, deterministic, behaviour-parameterized Python files. The LLM seam is a
fake, so nothing here spends provider credits.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import execute as ex  # noqa: E402
from mini_ork.cli import execute_handlers as exh  # noqa: E402
from mini_ork.workflow.compiler import WorkflowCompileError, compile_workflow  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_run_environment(monkeypatch):
    for name in (
        "MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_PLAN_PATH",
        "MINI_ORK_RUN_ID", "MO_TARGET_CWD", "MO_APPLY_IMPL_OUTPUT",
        "MINI_ORK_WORKFLOW", "MINI_ORK_RECIPE_ROOT", "MO_REVISE_ROUNDS",
        "MO_REVISE_TYPECHECK_BEHAVIOR", "MO_REVISE_TEST_BEHAVIOR",
        "MO_GRADE_RUN_REWARD", "MO_LEARNING_WRITEBACK", "MO_LANE_ROUTER",
    ):
        monkeypatch.delenv(name, raising=False)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, check=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "base")
    return repo


def _seed_db(tmp_path: Path, name: str) -> str:
    home = tmp_path / name / ".mini-ork"
    home.mkdir(parents=True)
    db = str(home / "state.db")
    subprocess.run(
        ["bash", str(REPO / "db" / "init.sh")],
        env={**os.environ, "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db},
        capture_output=True, text=True, check=True,
    )
    return db


def _seed_task_run(db: str, rid: str = "r") -> None:
    subprocess.run(
        ["sqlite3", db,
         f"INSERT INTO task_runs (id,task_class,workflow_version,kickoff_path,status,"
         f"cost_usd,created_at,updated_at) VALUES ('{rid}','code_fix','v1','k.md','planned',"
         f"0,strftime('%s','now','-60 seconds'),strftime('%s','now'));"],
        capture_output=True, text=True, check=True,
    )


_VERIFIER_SCRIPT = '''import json, os
run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
counter = os.path.join(run_dir, "{counter}") if run_dir else ""
n = 0
if counter:
    try:
        n = int(open(counter).read().strip())
    except Exception:
        n = 0
    try:
        open(counter, "w").write(str(n + 1))
    except OSError:
        pass
behavior = os.environ.get("{env}", "pass")
fail = behavior == "fail" or (behavior == "fail_first" and n == 0)
print(json.dumps({{"pass": not fail,
                   "reasons": ["{name}: synthetic round-0 failure"] if fail else []}}))
'''


def _base_workflow() -> dict:
    """A code-fix-shaped DAG: planner -> implementer -> {typecheck,test} ->
    reviewer, with the three ``retries`` edges looping back to the implementer
    and the three ``escalates_to`` rollback edges kept for after the rounds."""
    return {
        "version": "0.1.0",
        "task_class": "code_fix",
        # Parallel dispatch puts typecheck + test in the SAME readiness wave so
        # the shared-target grouping (DoD 4) is exercisable; serial dispatch
        # would run them one per wave and produce one round per source instead.
        "dispatch_mode": "parallel",
        "nodes": [
            {"name": "planner", "type": "planner", "dispatch_mode": "serial"},
            {"name": "implementer", "type": "implementer", "model_lane": "worker",
             "dispatch_mode": "serial"},
            {"name": "typecheck", "type": "verifier", "verifier_ref": "verifiers/typecheck.py",
             "dispatch_mode": "serial"},
            {"name": "test", "type": "verifier", "verifier_ref": "verifiers/test.py",
             "dispatch_mode": "serial"},
            {"name": "reviewer", "type": "reviewer", "model_lane": "reviewer",
             "dispatch_mode": "serial"},
            {"name": "rollback", "type": "rollback", "dispatch_mode": "serial"},
        ],
        "edges": [
            {"from": "planner", "to": "implementer", "edge_type": "depends_on"},
            {"from": "implementer", "to": "typecheck", "edge_type": "verifies"},
            {"from": "implementer", "to": "test", "edge_type": "verifies"},
            {"from": "typecheck", "to": "reviewer", "edge_type": "depends_on"},
            {"from": "test", "to": "reviewer", "edge_type": "depends_on"},
            {"from": "typecheck", "to": "rollback", "edge_type": "escalates_to"},
            {"from": "test", "to": "rollback", "edge_type": "escalates_to"},
            {"from": "reviewer", "to": "rollback", "edge_type": "escalates_to"},
            {"from": "typecheck", "to": "implementer", "edge_type": "retries", "max_rounds": 2},
            {"from": "test", "to": "implementer", "edge_type": "retries", "max_rounds": 2},
            {"from": "reviewer", "to": "implementer", "edge_type": "retries", "max_rounds": 2},
        ],
    }


def _write_overlay(tmp_path: Path, workflow: dict) -> Path:
    """Write the recipe overlay (workflow + two parameterized verifier scripts)."""
    recipe_dir = tmp_path / "overlay" / "recipes" / "revise-test"
    (recipe_dir / "verifiers").mkdir(parents=True)
    (recipe_dir / "workflow.yaml").write_text(yaml.safe_dump(workflow), encoding="utf-8")
    (recipe_dir / "verifiers" / "typecheck.py").write_text(
        _VERIFIER_SCRIPT.format(counter=".revise-typecheck-count",
                                env="MO_REVISE_TYPECHECK_BEHAVIOR", name="typecheck"),
        encoding="utf-8")
    (recipe_dir / "verifiers" / "test.py").write_text(
        _VERIFIER_SCRIPT.format(counter=".revise-test-count",
                                env="MO_REVISE_TEST_BEHAVIOR", name="test"),
        encoding="utf-8")
    return recipe_dir / "workflow.yaml"


def _run(tmp_path, monkeypatch, *, workflow, reviewer_verdicts,
         typecheck="pass", test="pass", revise_rounds=None):
    """Drive ``ex.main`` once and return (rc, state, run_dir)."""
    wf_path = _write_overlay(tmp_path, workflow)
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plan = run_dir / "plan.json"
    plan.write_text(json.dumps({"objective": "o", "task_class": "code_fix"}))
    db = _seed_db(tmp_path, "db")
    _seed_task_run(db)

    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    monkeypatch.setenv("MINI_ORK_WORKFLOW", str(wf_path))
    monkeypatch.setenv("MINI_ORK_PLAN_PATH", str(plan))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "r")
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "db" / ".mini-ork"))
    monkeypatch.setenv("MINI_ORK_RECIPE_ROOT", str(tmp_path / "overlay"))
    monkeypatch.setenv("MINI_ORK_RECIPE", "revise-test")
    monkeypatch.setenv("MO_REVISE_TYPECHECK_BEHAVIOR", typecheck)
    monkeypatch.setenv("MO_REVISE_TEST_BEHAVIOR", test)
    if revise_rounds is None:
        monkeypatch.delenv("MO_REVISE_ROUNDS", raising=False)
    else:
        monkeypatch.setenv("MO_REVISE_ROUNDS", revise_rounds)
    # Cheap post-run learning is irrelevant here; disable to keep the test fast
    # and deterministic (they re-scan the whole seeded DB).
    monkeypatch.setenv("MO_GRADE_RUN_REWARD", "0")
    monkeypatch.setenv("MO_LEARNING_WRITEBACK", "0")
    monkeypatch.setenv("MO_LANE_ROUTER", "0")

    state = {"implementer": 0, "reviewer": 0, "rollback": 0, "impl_prompts": []}

    def fake_dispatch(task_class, lane, prompt):
        if "Implement:" in prompt:
            state["implementer"] += 1
            state["impl_prompts"].append(prompt)
            (repo / "a.py").write_text("x = 2\n")
            return 0, "done"
        if "Review the implementation" in prompt:
            state["reviewer"] += 1
            verdict = reviewer_verdicts[min(state["reviewer"], len(reviewer_verdicts)) - 1]
            if verdict == "pass":
                return 0, json.dumps({"verdict": "pass", "notes": []})
            return 0, json.dumps(
                {"verdict": "needs_revision", "notes": ["the fix is incomplete"]})
        return 0, "done"

    real_rollback = exh.NODE_HANDLER_REGISTRY["rollback"]

    def counting_rollback(ctx):
        state["rollback"] += 1
        return real_rollback(ctx)

    monkeypatch.setitem(exh.NODE_HANDLER_REGISTRY, "rollback", counting_rollback)

    rc = ex.main(argv=[], root=str(REPO), dispatch_fn=fake_dispatch)
    return rc, state, run_dir


# ── DoD 1: recovers ───────────────────────────────────────────────────────────

def test_recovers_reviewer_fails_then_passes(tmp_path, monkeypatch):
    rc, state, run_dir = _run(
        tmp_path, monkeypatch, workflow=_base_workflow(),
        reviewer_verdicts=["needs_revision", "pass"],
    )

    assert state["implementer"] == 2          # round 0 + revise round 1
    assert state["rollback"] == 0             # nothing left failed
    assert rc == 0
    feedback = (run_dir / "revise" / "round-1.md").read_text()
    assert "the fix is incomplete" in feedback   # reviewer note text
    assert "Revision round 1" in state["impl_prompts"][1]
    assert "Revision round" not in state["impl_prompts"][0]


# ── DoD 2: exhausts ───────────────────────────────────────────────────────────

def test_exhausts_reviewer_always_fails(tmp_path, monkeypatch):
    rc, state, run_dir = _run(
        tmp_path, monkeypatch, workflow=_base_workflow(),
        reviewer_verdicts=["needs_revision", "needs_revision", "needs_revision"],
    )

    assert state["implementer"] == 3          # 1 + 2 rounds
    assert state["rollback"] == 1             # exactly once, at the end
    assert rc != 0
    assert (run_dir / "revise" / "round-1.md").exists()
    assert (run_dir / "revise" / "round-2.md").exists()
    assert not (run_dir / "revise" / "round-3.md").exists()


# ── DoD 3: opt-out ────────────────────────────────────────────────────────────

def test_opt_out_with_zero_rounds(tmp_path, monkeypatch):
    rc, state, _run_dir = _run(
        tmp_path, monkeypatch, workflow=_base_workflow(),
        reviewer_verdicts=["needs_revision", "needs_revision"],
        revise_rounds="0",
    )

    assert state["implementer"] == 1          # no revise round
    assert state["rollback"] == 1
    assert rc != 0


# ── DoD 4: one round for a shared target ──────────────────────────────────────

def test_shared_target_one_round_with_both_sections(tmp_path, monkeypatch):
    rc, state, run_dir = _run(
        tmp_path, monkeypatch, workflow=_base_workflow(),
        reviewer_verdicts=["pass"], typecheck="fail_first", test="fail_first",
    )

    assert state["implementer"] == 2
    assert state["rollback"] == 0
    assert rc == 0
    feedback = (run_dir / "revise" / "round-1.md").read_text()
    assert "typecheck" in feedback and "test" in feedback   # both sections
    assert not (run_dir / "revise" / "round-2.md").exists()


# ── DoD 5: edges matter ───────────────────────────────────────────────────────

def test_failing_node_without_retries_edge_gets_no_round(tmp_path, monkeypatch):
    workflow = _base_workflow()
    workflow["edges"] = [e for e in workflow["edges"]
                         if not (e["edge_type"] == "retries" and e["from"] == "typecheck")]
    rc, state, run_dir = _run(
        tmp_path, monkeypatch, workflow=workflow,
        reviewer_verdicts=["pass"], typecheck="fail", test="pass",
    )

    assert state["implementer"] == 1          # typecheck has no retries edge
    assert state["rollback"] == 1
    assert rc != 0
    assert not (run_dir / "revise" / "round-1.md").exists()


# ── DoD 6: archive ────────────────────────────────────────────────────────────

def test_prior_verifier_evidence_is_archived(tmp_path, monkeypatch):
    rc, state, run_dir = _run(
        tmp_path, monkeypatch, workflow=_base_workflow(),
        reviewer_verdicts=["pass"], typecheck="fail_first", test="pass",
    )

    assert rc == 0
    archived = run_dir / "revise" / "round-1" / "verifier_typecheck.json"
    assert archived.exists()
    archived_data = json.loads(archived.read_text())
    assert archived_data.get("pass") is False     # the PRIOR (failing) evidence


# ── DoD 7: compiler ───────────────────────────────────────────────────────────

def _compile_yaml(tmp_path, workflow):
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump(workflow), encoding="utf-8")
    return compile_workflow(str(p))


def test_compiler_populates_retry_edges(tmp_path):
    cw = _compile_yaml(tmp_path, _base_workflow())
    assert cw.retry_edges == {
        "typecheck": ("implementer", 2),
        "test": ("implementer", 2),
        "reviewer": ("implementer", 2),
    }


def test_compiler_rejects_non_ancestor_target(tmp_path):
    workflow = _base_workflow()
    workflow["edges"].append(
        {"from": "reviewer", "to": "test", "edge_type": "retries"})  # test is not an ancestor
    with pytest.raises(WorkflowCompileError):
        _compile_yaml(tmp_path, workflow)


def test_compiler_rejects_second_retries_edge(tmp_path):
    workflow = _base_workflow()
    workflow["edges"].append(
        {"from": "reviewer", "to": "implementer", "edge_type": "retries", "max_rounds": 2})
    with pytest.raises(WorkflowCompileError):
        _compile_yaml(tmp_path, workflow)


def test_compiler_rejects_negative_max_rounds(tmp_path):
    workflow = _base_workflow()
    workflow["edges"] = [dict(e, max_rounds=-1) if e["edge_type"] == "retries" else e
                         for e in workflow["edges"]]
    with pytest.raises(WorkflowCompileError):
        _compile_yaml(tmp_path, workflow)


# ── DoD 8: real recipes ───────────────────────────────────────────────────────

def test_real_recipes_compile_and_expose_retry_edges():
    for recipe, sources in (
        ("code-fix", {"typecheck", "test", "reviewer"}),
        ("framework-edit", {"static_check_verifier", "test_verifier", "reviewer"}),
    ):
        cw = compile_workflow(str(REPO / "recipes" / recipe / "workflow.yaml"))
        assert set(cw.retry_edges) == sources
        for source, (target, max_rounds) in cw.retry_edges.items():
            assert target == "implementer"
            assert max_rounds == 2
