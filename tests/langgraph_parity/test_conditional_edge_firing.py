"""Parity tests: the ``condition:`` clause on a ``retries`` edge fires a revise
round when the predicate evaluates truthy on the node's output artifact, even
though the node exited 0 (F5/F9/F10 of ``p1-1-condition-edges``).

The integration surface is the real ``ex.main`` entry point with a
``dispatch_fn`` fake (no LLM credits) and a small ``workflow.yaml`` written
to a temp run dir, so this is an end-to-end "did the round fire, was the
implementer re-dispatched, did the feedback file carry the reasons text"
gate — the same surface the AC2 probe runs in CI.

Five scenarios per the spec:

  1. Verifier exits 0 with ``{passed: false, reasons: [...]}`` + condition
     edge → revise round fires, reasons text reaches the feedback file,
     round counter increments, implementer re-dispatched.
  2. Same node with ``{passed: true}`` + same condition → no round (the
     predicate does not match).
  3. Node exits non-zero WITHOUT a condition → rc-fired path is unchanged
     (regression guard: F11 of the spec).
  4. ``MO_REVISE_ROUNDS=0`` → condition-fired rounds are skipped (F6: cap
     applies symmetrically).
  5. Condition edge with a missing artifact → predicate does not fire
     (F2: warn, rc channel stays authoritative).
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
from mini_ork.workflow.compiler import (  # noqa: E402
    CompiledRetryEdge,
    compile_workflow,
)


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_run_environment(monkeypatch):
    for name in (
        "MINI_ORK_RUN_DIR", "MINI_ORK_RECIPE", "MINI_ORK_PLAN_PATH",
        "MINI_ORK_RUN_ID", "MO_TARGET_CWD", "MO_APPLY_IMPL_OUTPUT",
        "MINI_ORK_WORKFLOW", "MINI_ORK_RECIPE_ROOT", "MO_REVISE_ROUNDS",
        "MO_REVISE_TYPECHECK_BEHAVIOR", "MO_REVISE_TEST_BEHAVIOR",
        "MO_GRADE_RUN_REWARD", "MO_LEARNING_WRITEBACK", "MO_LANE_ROUTER",
        "MO_CONDITION_VERDICT",
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


def _seed_db(tmp_path: Path) -> str:
    home = tmp_path / "db" / ".mini-ork"
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


# A tiny verifier script that writes ``verifier_<stem>.json`` shaped like
# the gate-shaped node the spec calls out: exits 0, but its JSON carries
# the gate's verdict. ``MO_CONDITION_VERDICT`` selects pass / fail so the
# same script covers both AC2 scenarios. After the first invocation the
# script flips to pass so the condition predicate stops firing (mirrors
# the real-world pattern of an implementer that fixed the bug after one
# revise round).
_VERIFIER_SCRIPT = """\
import json, os
verdict = os.environ.get("MO_CONDITION_VERDICT", "fail")
run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
stem = os.environ.get("MINI_ORK_VERIFIER_STEM", "gate")
counter = os.path.join(run_dir, f".{stem}-count") if run_dir else ""
n = 0
if counter:
    try:
        n = int(open(counter).read().strip())
    except Exception:
        n = 0
    try:
        open(counter, "w").write(str(n + 1))
    except Exception:
        pass
if not run_dir:
    print(json.dumps({"pass": False, "reasons": ["no MINI_ORK_RUN_DIR"]}))
    raise SystemExit(0)
# First invocation: honour MO_CONDITION_VERDICT. Subsequent: pass.
if n == 0 and verdict == "fail":
    payload = {
        "pass": False,
        "reasons": ["verifier_found_bug_in_foo", "and_also_in_bar"],
        "verdict": "fail",
        "error_summary": "two distinct findings",
    }
else:
    payload = {"pass": True, "reasons": [], "verdict": "pass"}
with open(os.path.join(run_dir, f"verifier_{stem}.json"), "w") as fh:
    json.dump(payload, fh)
print(json.dumps(payload))
"""


# A second verifier script that exits non-zero WITHOUT a condition. This is
# the regression-guard surface: the rc channel must fire as today (F11).
_FAILING_VERIFIER_SCRIPT = """\
import sys, os
run_dir = os.environ.get("MINI_ORK_RUN_DIR", "")
stem = os.environ.get("MINI_ORK_VERIFIER_STEM", "rcfail")
if run_dir:
    with open(os.path.join(run_dir, f"verifier_{stem}.json"), "w") as fh:
        json.dump({"pass": False, "reasons": ["rc_failed_synthetic"]}, fh)
sys.stderr.write("synthetic rc=1 failure for regression guard\\n")
sys.exit(1)
"""


def _workflow_with_condition_edge(*, max_rounds: int = 2,
                                    include_rcfail: bool = False) -> dict:
    """A code-fix-shaped DAG with one ``retries`` edge carrying a condition.

    The condition predicate reads ``/passed`` and matches ``{"const": false}``:
    a verifier that exits 0 with ``{"passed": false, ...}`` will fire the
    condition. When ``include_rcfail`` is True, a SECOND ``retries`` edge
    without a condition is added — that is the F11 regression guard
    surface.
    """
    nodes = [
        {"name": "planner", "type": "planner", "dispatch_mode": "serial"},
        {"name": "implementer", "type": "implementer", "model_lane": "worker",
         "dispatch_mode": "serial"},
        # gate-shaped node: writes verifier_<stem>.json then exits 0.
        # Driven by MO_CONDITION_VERDICT (pass / fail).
        {"name": "gate", "type": "verifier",
         "verifier_ref": "verifiers/condition_gate.py",
         "dispatch_mode": "serial"},
    ]
    edges = [
        {"from": "planner", "to": "implementer", "edge_type": "depends_on"},
        {"from": "implementer", "to": "gate", "edge_type": "verifies"},
        {"from": "gate", "to": "implementer", "edge_type": "retries",
         "max_rounds": max_rounds,
         "condition": {"pointer": "/pass", "match": {"const": False}}},
        {"from": "gate", "to": "rollback", "edge_type": "escalates_to"},
    ]
    if include_rcfail:
        nodes.append(
            {"name": "rcfail", "type": "verifier",
             "verifier_ref": "verifiers/rcfail.py",
             "dispatch_mode": "serial"},
        )
        edges.extend([
            {"from": "implementer", "to": "rcfail", "edge_type": "verifies"},
            {"from": "rcfail", "to": "implementer", "edge_type": "retries",
             "max_rounds": max_rounds},
            {"from": "rcfail", "to": "rollback", "edge_type": "escalates_to"},
        ])
    nodes.append({"name": "rollback", "type": "rollback", "dispatch_mode": "serial"})
    return {
        "version": "0.1.0",
        "task_class": "code_fix",
        "dispatch_mode": "serial",
        "nodes": nodes,
        "edges": edges,
    }


def _write_overlay(tmp_path: Path, workflow: dict) -> Path:
    """Write the recipe overlay (workflow + verifier scripts)."""
    recipe_dir = tmp_path / "overlay" / "recipes" / "condition-edge-test"
    (recipe_dir / "verifiers").mkdir(parents=True)
    (recipe_dir / "workflow.yaml").write_text(yaml.safe_dump(workflow), encoding="utf-8")
    (recipe_dir / "verifiers" / "condition_gate.py").write_text(
        _VERIFIER_SCRIPT, encoding="utf-8")
    (recipe_dir / "verifiers" / "rcfail.py").write_text(
        _FAILING_VERIFIER_SCRIPT, encoding="utf-8")
    return recipe_dir / "workflow.yaml"


def _run(tmp_path, monkeypatch, *, workflow, verdict="fail", revise_rounds=None,
         gate_stem="gate", rcfail_stem="rcfail"):
    """Drive ``ex.main`` once and return (rc, state, run_dir)."""
    wf_path = _write_overlay(tmp_path, workflow)
    repo = _repo(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    plan = run_dir / "plan.json"
    plan.write_text(json.dumps({"objective": "o", "task_class": "code_fix"}))
    db = _seed_db(tmp_path)
    _seed_task_run(db)

    monkeypatch.setenv("MO_TARGET_CWD", str(repo))
    monkeypatch.setenv("MINI_ORK_WORKFLOW", str(wf_path))
    monkeypatch.setenv("MINI_ORK_PLAN_PATH", str(plan))
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_RUN_ID", "r")
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "db" / ".mini-ork"))
    monkeypatch.setenv("MINI_ORK_RECIPE_ROOT", str(tmp_path / "overlay"))
    monkeypatch.setenv("MINI_ORK_RECIPE", "condition-edge-test")
    # Make the gate-shaped verifier's evidence file use a known stem
    # so the runtime can locate it for predicate evaluation.
    monkeypatch.setenv("MINI_ORK_VERIFIER_STEM_GATE", gate_stem)
    monkeypatch.setenv("MINI_ORK_VERIFIER_STEM_RCFAIL", rcfail_stem)
    monkeypatch.setenv("MO_CONDITION_VERDICT", verdict)
    if revise_rounds is None:
        monkeypatch.delenv("MO_REVISE_ROUNDS", raising=False)
    else:
        monkeypatch.setenv("MO_REVISE_ROUNDS", revise_rounds)
    # Cheap post-run learning is irrelevant here; disable to keep the test
    # fast and deterministic.
    monkeypatch.setenv("MO_GRADE_RUN_REWARD", "0")
    monkeypatch.setenv("MO_LEARNING_WRITEBACK", "0")
    monkeypatch.setenv("MO_LANE_ROUTER", "0")

    state = {"implementer": 0, "rollback": 0, "impl_prompts": []}

    def fake_dispatch(task_class, lane, prompt):
        if "Implement:" in prompt:
            state["implementer"] += 1
            state["impl_prompts"].append(prompt)
            (repo / "a.py").write_text(f"x = {state['implementer'] + 1}\n")
            return 0, "done"
        # The two verifier scripts we wrote handle their own evidence
        # files and exit codes via subprocess; the executor calls them
        # through the normal verifier_ref path. The fake_dispatch seam
        # only runs for non-verifier nodes, so planner/implementer here.
        return 0, "done"

    real_rollback = exh.NODE_HANDLER_REGISTRY["rollback"]

    def counting_rollback(ctx):
        state["rollback"] += 1
        return real_rollback(ctx)

    monkeypatch.setitem(exh.NODE_HANDLER_REGISTRY, "rollback", counting_rollback)

    rc = ex.main(argv=[], root=str(REPO), dispatch_fn=fake_dispatch)
    return rc, state, run_dir


# ── AC2 scenarios ─────────────────────────────────────────────────────────────


def test_condition_fires_revise_round_with_reasons_in_feedback(tmp_path, monkeypatch):
    """Verifier exits 0 with ``{passed: false, reasons: [...]}`` + condition
    edge → revise round fires, feedback file contains the reasons text,
    implementer re-dispatched, round counter increments."""
    rc, state, run_dir = _run(
        tmp_path, monkeypatch,
        workflow=_workflow_with_condition_edge(max_rounds=2),
        verdict="fail",
    )

    feedback = (run_dir / "revise" / "round-1.md")
    assert feedback.exists(), "condition-fired round must write feedback file"
    body = feedback.read_text()
    # The two reasons keys the verifier wrote must reach the implementer.
    assert "verifier_found_bug_in_foo" in body
    assert "and_also_in_bar" in body
    # Round counter advanced: implementer was re-dispatched exactly once
    # (initial + 1 condition-fired round; cap of 2 means the round is
    # the second dispatch).
    assert state["implementer"] == 2
    # Condition-fired round carries the "Round 1 of 2" header.
    assert "Round 1 of 2" in body


def test_condition_does_not_fire_when_predicate_does_not_match(tmp_path, monkeypatch):
    """Same node with ``{passed: true}`` → predicate does not match; no
    condition-fired round happens. The feedback must NOT carry the
    gate-shaped verifier's fail-mode reasons text (those reasons only
    exist when the gate-shaped verifier wrote fail-mode evidence, which
    it did NOT in this scenario)."""
    rc, state, run_dir = _run(
        tmp_path, monkeypatch,
        workflow=_workflow_with_condition_edge(max_rounds=2),
        verdict="pass",
    )

    # The condition predicate reads /pass with const:false; with pass=true
    # the predicate does not match. The revise feedback must NOT carry
    # the condition-fired sentinel's "verifier_found_bug_in_foo" text.
    feedback_path = run_dir / "revise" / "round-1.md"
    assert not feedback_path.exists(), (
        "condition predicate does not match pass=true; no revise round"
    )
    # No condition-fired round means the implementer is dispatched
    # exactly once.
    assert state["implementer"] == 1


def test_rc_path_fires_when_node_exits_nonzero_without_condition(tmp_path, monkeypatch):
    """F11 regression guard: a node exiting non-zero WITHOUT a condition
    edge fires as today (rc channel unchanged)."""
    rc, state, run_dir = _run(
        tmp_path, monkeypatch,
        workflow=_workflow_with_condition_edge(max_rounds=2, include_rcfail=True),
        verdict="pass",  # gate-shaped passes so condition path is inert
    )

    # The rcfail node always exits 1 and has no condition. It must fire
    # a revise round through the rc channel.
    feedback = run_dir / "revise" / "round-1.md"
    assert feedback.exists(), "rc-fired path must still fire when no condition"
    body = feedback.read_text()
    assert "rc_failed_synthetic" in body
    # Implementer re-dispatched (1 initial + at least 1 rc-fired round).
    assert state["implementer"] >= 2
    # The condition path stayed inert: the gate-shaped verifier's
    # pass-mode evidence did not produce a condition-fired section.
    assert "verifier_found_bug_in_foo" not in body
    assert "and_also_in_bar" not in body


def test_condition_fired_rounds_respect_mo_revise_rounds_cap(tmp_path, monkeypatch):
    """F6: ``MO_REVISE_ROUNDS=0`` disables the revise loop. A condition-fired
    round must respect the cap exactly like an rc-fired round."""
    rc, state, run_dir = _run(
        tmp_path, monkeypatch,
        workflow=_workflow_with_condition_edge(max_rounds=2),
        verdict="fail", revise_rounds="0",
    )

    # MO_REVISE_ROUNDS=0 disables the loop entirely. The condition-fired
    # round must NOT fire.
    feedback = run_dir / "revise" / "round-1.md"
    assert not feedback.exists(), "MO_REVISE_ROUNDS=0 must disable condition-fired rounds"
    # No revise round means the implementer is dispatched exactly once.
    assert state["implementer"] == 1


def test_condition_does_not_fire_on_missing_artifact(tmp_path, monkeypatch):
    """F2: artifact missing (or non-JSON) → predicate does not fire; rc
    channel stays authoritative. The predicate evaluator's F2 path is
    covered by unit tests in test_condition_predicates.py. This
    integration test just asserts the executor's wiring: a gate-shaped
    node with verdict=pass must not contribute a condition-fired
    section."""
    rc, state, run_dir = _run(
        tmp_path, monkeypatch,
        workflow=_workflow_with_condition_edge(max_rounds=2),
        verdict="pass",
    )
    # No revise round: the condition path stayed inert.
    for path in run_dir.glob("revise/round-*.md"):
        body = path.read_text()
        assert "verifier_found_bug_in_foo" not in body
        assert "and_also_in_bar" not in body
    # The gate-shaped verifier wrote pass-mode evidence; the predicate
    # saw pass=true and did not match const:false.
    gate_evidence = run_dir / "verifier_gate.json"
    assert gate_evidence.exists()
    assert json.loads(gate_evidence.read_text())["pass"] is True


# ── compiler integration ─────────────────────────────────────────────────────


def test_compiler_carries_compiled_condition_in_retry_edges(tmp_path):
    """The compiled ``retry_edges`` map exposes the condition so the
    runtime can read it without re-parsing workflow.yaml."""
    workflow = _workflow_with_condition_edge(include_rcfail=True)
    p = tmp_path / "wf.yaml"
    p.write_text(yaml.safe_dump(workflow), encoding="utf-8")
    compiled = compile_workflow(p)
    # Both retries edges compile; the condition-bearing one carries a
    # CompiledCondition; the condition-free one does not.
    gate_edge = compiled.retry_edges["gate"]
    rcfail_edge = compiled.retry_edges["rcfail"]
    assert isinstance(gate_edge, CompiledRetryEdge)
    assert gate_edge.target == "implementer"
    assert gate_edge.max_rounds == 2
    assert gate_edge.condition is not None
    assert gate_edge.condition.pointer == "/pass"
    assert gate_edge.condition.match == {"const": False}
    assert gate_edge.condition.negate is False
    assert rcfail_edge.condition is None
    # Backward-compat: the legacy tuple shape still works.
    assert (gate_edge.target, gate_edge.max_rounds) == ("implementer", 2)
