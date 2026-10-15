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
fail = behavior in ("fail", "fail_toolchain") or (behavior == "fail_first" and n == 0)
payload = {{"pass": not fail,
            "reasons": ["{name}: synthetic round-0 failure"] if fail else []}}
if behavior == "fail_toolchain":
    payload["failure_kind"] = "toolchain"
print(json.dumps(payload))
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
         typecheck="pass", test="pass", revise_rounds=None, recovery_from=None):
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
    # A partial-closure recovery: restrict dispatch to the DAG downstream of
    # `recovery_from` (e.g. a `recover --from-node test` skips the implementer).
    if recovery_from:
        monkeypatch.setenv("MINI_ORK_RECOVERY_FROM", recovery_from)

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


# ── §failure_kind: a toolchain abstention is not a failure ────────────────────

def test_abstained_verifier_withholds_and_keeps_the_edit(tmp_path, monkeypatch):
    """A verifier that could not run its tool (``failure_kind: toolchain``)
    abstains — the audit's whole point: it must NOT send findings back to the
    implementer (no revise round), must NOT roll back, and must withhold the
    publish with rc 0. The control below shows the same fixture WITHOUT the kind
    still fails and rolls back."""
    monkeypatch.setenv("MO_LEVEL_VECTOR", "1")  # the run verdict carries failure_kinds
    rc, state, run_dir = _run(
        tmp_path, monkeypatch, workflow=_base_workflow(),
        reviewer_verdicts=["pass"], typecheck="fail_toolchain", test="pass",
    )
    assert rc == 0
    assert state["rollback"] == 0
    assert state["implementer"] == 1
    assert not (run_dir / "revise" / "round-1.md").exists()

    verdict = json.loads((run_dir / "verdict.json").read_text())
    assert verdict["verdict"] == "abstain"
    assert verdict["failed_nodes"] == 0
    assert verdict["failure_kinds"] == ["toolchain"]


def test_control_without_failure_kind_still_fails_and_rolls_back(tmp_path, monkeypatch):
    # No failure_kind: the identical failing verifier is a real failure — the
    # fail-closed half. MO_REVISE_ROUNDS=0 keeps the retries edge from absorbing
    # it into a revise round, so the terminal failure is what we observe.
    rc, state, run_dir = _run(
        tmp_path, monkeypatch, workflow=_base_workflow(),
        reviewer_verdicts=["pass"], typecheck="fail", test="pass",
        revise_rounds="0",
    )
    assert rc != 0
    assert state["rollback"] == 1
    verdict = json.loads((run_dir / "verdict.json").read_text())
    assert verdict["failed_nodes"] >= 1


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
        ("framework-edit", {"static_check_verifier", "test_verifier",
                            "kickoff_contract_verifier", "spec_test_verifier",
                            "reviewer"}),
    ):
        cw = compile_workflow(str(REPO / "recipes" / recipe / "workflow.yaml"))
        assert set(cw.retry_edges) == sources
        for source, (target, max_rounds) in cw.retry_edges.items():
            assert target == "implementer"
            assert max_rounds == 2


# ── escalation wiring: a `recursion:` block turns `escalates_to` into a repair ─

def _escalation_workflow(**extra) -> dict:
    """A loop-shaped DAG: verify -> synth, with an escalation to a reflector
    that feeds a replanner, whose `recursive` edge re-enters at the implementer.

    Mirrors ``recipes/recursive-validate-impl``: the escalation target is a
    FORWARD node (not an ancestor), which is why it cannot be a ``retries``
    edge and needs the separate map.
    """
    return {
        "version": "0.2.0",
        "task_class": "loop_demo",
        "nodes": [
            {"name": "planner", "type": "planner", "dispatch_mode": "serial"},
            {"name": "implementer", "type": "implementer", "dispatch_mode": "serial"},
            {"name": "tier1", "type": "verifier", "verifier_ref": "v.py",
             "dispatch_mode": "serial"},
            {"name": "synth", "type": "reviewer", "dispatch_mode": "serial"},
            {"name": "reflector", "type": "reflector", "dispatch_mode": "serial"},
            {"name": "replanner", "type": "planner", "dispatch_mode": "serial"},
        ],
        "edges": [
            {"from": "planner", "to": "implementer", "edge_type": "depends_on"},
            {"from": "implementer", "to": "tier1", "edge_type": "verifies"},
            {"from": "tier1", "to": "synth", "edge_type": "depends_on"},
            {"from": "synth", "to": "reflector", "edge_type": "escalates_to",
             "condition": "fail"},
            {"from": "reflector", "to": "replanner", "edge_type": "depends_on"},
            {"from": "replanner", "to": "implementer", "edge_type": "depends_on",
             "recursive": True},
        ],
        **extra,
    }


def test_escalates_to_stays_inert_without_a_recursion_block(tmp_path):
    """The opt-out is the declaration. 30+ recipes carry `escalates_to` edges
    that mean "a human handles this"; none of them may start self-repairing."""
    cw = _compile_yaml(tmp_path, _escalation_workflow())
    assert cw.escalation_edges == {}
    assert cw.recursive_children == {}


def test_recursion_block_publishes_the_escalation_loop(tmp_path):
    """With the block, the escalation is a repair channel and the loop's
    back-edge is published so the reset can reach the loop body."""
    cw = _compile_yaml(tmp_path, _escalation_workflow(recursion={
        "max_iterations": 5,
        "convergence_check": "all_dod_probes_pass",
        "budget_cap_per_iter_usd": 20.0,
        "budget_cap_total_usd": 60.0,
        "divergence_kill": "same signature twice -> escalate",
    }))
    assert cw.escalation_edges["synth"].target == "reflector"
    assert cw.escalation_edges["synth"].max_rounds == 5   # the declared cap
    assert cw.recursive_children == {"replanner": ("implementer",)}
    # Still not a `retries` edge: the two maps stay apart so the ancestor
    # invariant `retries` is validated against is not weakened.
    assert "synth" not in cw.retry_edges


def test_retries_edge_outranks_an_escalation_on_the_same_source(tmp_path):
    """A source that names its own revise target and cap wins outright; the
    escalation must not silently replace it."""
    workflow = _escalation_workflow(recursion={
        "max_iterations": 5,
        "convergence_check": "all_dod_probes_pass",
        "budget_cap_per_iter_usd": 20.0,
        "budget_cap_total_usd": 60.0,
        "divergence_kill": "n/a",
    })
    workflow["edges"].append(
        {"from": "synth", "to": "implementer", "edge_type": "retries",
         "max_rounds": 3})
    cw = _compile_yaml(tmp_path, workflow)
    assert cw.retry_edges["synth"].target == "implementer"
    assert cw.retry_edges["synth"].max_rounds == 3
    assert "synth" not in cw.escalation_edges


def test_recursive_validate_impl_declares_a_usable_loop():
    """The recipe whose panel escalation died with no repair round
    (run-1791798883-27913) must compile to an executable one."""
    cw = compile_workflow(str(REPO / "recipes" / "recursive-validate-impl" / "workflow.yaml"))
    assert cw.escalation_edges["tier4_synth"].target == "reflector"
    assert cw.escalation_edges["tier4_synth"].max_rounds == 5
    assert "implementer" in cw.recursive_children["replanner"]
    # Type-1/2/3 verifier failures escalate the same way.
    for tier in ("tier1_compile", "tier2_unit", "tier3_property"):
        assert cw.escalation_edges[tier].target == "reflector"


# ── DoD 9: a revise target OUTSIDE the closure is skipped, not fatal ──────────

def test_revise_target_outside_closure_is_skipped(tmp_path, monkeypatch):
    """A partial-closure recovery whose revise target is NOT dispatched must not
    crash the revise loop.

    ``recover --from-node test`` restricts dispatch to the DAG downstream of
    ``test`` — the implementer is not part of the closure. When the reviewer then
    fails, its ``retries`` edge points back to the implementer, which has no
    entry in ``fields_by_id``; the old code indexed it anyway and died with
    ``KeyError: 'implementer'`` at ``[fields_by_id[n] for n in reset_ids]``.
    A revise is impossible here, so it must be SKIPPED and the failure left to
    stand. Regression for run-1791469896-2926 (a from-test recovery crashed)."""
    rc, state, _run_dir = _run(
        tmp_path, monkeypatch, workflow=_base_workflow(),
        reviewer_verdicts=["needs_revision"],
        recovery_from="test",
    )

    assert state["implementer"] == 0   # outside the closure → never dispatched
    assert rc != 0                      # the failure stands; no crash
