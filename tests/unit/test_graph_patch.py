"""Unit tests for SDE-5a typed run-graph patches.

Hermetic: every test builds ``<tmp>/home/runs/<run_id>`` and pins the workflow
through ``MINI_ORK_HOME`` / ``MINI_ORK_WORKFLOW``; nothing reads or writes the
live run directory, and an injected ``spawn`` keeps ``apply`` from launching a
real recover.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import mini_ork
import mini_ork.cli.execute as execute
import mini_ork.observability.node_events as node_events
import mini_ork.orchestration.runner_orchestrator as runner_orchestrator
import mini_ork.recovery.graph_patch as gp
import pytest
import yaml
from mini_ork.orchestration.runner_orchestrator import Action
from mini_ork.workflow.compiler import compile_workflow

REPO_ROOT = Path(mini_ork.__file__).resolve().parents[1]
RUNSCOPED_ENV = (
    "MINI_ORK_RUN_DIR", "MINI_ORK_WORKFLOW", "MINI_ORK_RECIPE",
    "MINI_ORK_HOME", "MINI_ORK_ROOT", "MINI_ORK_DB", "MO_AUTO_REPAIR",
)
FAKE_PID = 4242

BASE_WORKFLOW = """\
nodes:
  - name: planner
    type: planner
  - name: implementer
    type: implementer
    model_lane: heavy
  - name: reviewer
    type: reviewer
    model_lane: light
edges:
  - from: planner
    to: implementer
  - from: implementer
    to: reviewer
"""


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for var in RUNSCOPED_ENV:
        monkeypatch.delenv(var, raising=False)


def make_run(tmp_path, run_id="r1", with_repair_json=True):
    home = tmp_path / "home"
    run_dir = home / "runs" / run_id
    (run_dir / "graph-patches").mkdir(parents=True)
    workflow = home / "workflow.yaml"
    workflow.write_text(BASE_WORKFLOW, encoding="utf-8")
    if with_repair_json:
        (run_dir / "repair.json").write_text(json.dumps({
            "state": "repairing",
            "attempts": [{"from_node": "implementer"}],
            "kept_marker": "keep-me",
        }), encoding="utf-8")
    return home, run_dir, workflow


def pin_env(monkeypatch, home, workflow):
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    monkeypatch.setenv("MINI_ORK_WORKFLOW", str(workflow))


def load_yaml(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class FakeSpawn:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return FAKE_PID


class _FakeReply:
    def __init__(self, text):
        self.ok = True
        self.text = text
        self.error = ""
        self.cost_usd = 0.0


class _FakeDispatch:
    def __init__(self, text):
        self.text = text
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return _FakeReply(self.text)


def prime_replan(home, run_id, patch, base_document, *names):
    """Accept a replan chain through the injected planner lane.

    The chain clones ``BASE_WORKFLOW``'s implementer template (which declares
    no ``prompt_ref``), so the triple matches that base node exactly.
    """
    nodes = [{"name": name, "type": "implementer", "model_lane": "heavy"}
             for name in names]
    dispatch = _FakeDispatch(json.dumps({"nodes": nodes}))
    return gp._replan_chain(str(home), run_id, patch, base_document,
                            dispatch=dispatch)


def propose_rerun(run_dir, from_node="implementer", reason="re-verify after flake"):
    return gp.propose(str(run_dir), {"kind": "rerun", "from_node": from_node,
                                     "reason": reason, "source": "operator"})


# ── propose ────────────────────────────────────────────────────────────────


def test_propose_numbers_sequentially_and_dedups_rerun(tmp_path):
    home, run_dir, _ = make_run(tmp_path)
    first = propose_rerun(run_dir, "implementer")
    assert first == 1
    assert propose_rerun(run_dir, "implementer") == 1  # identical, unapplied
    assert propose_rerun(run_dir, "reviewer", "second failure") == 2
    assert sorted(p.name for p in (run_dir / "graph-patches").glob("*.yaml")) == [
        "001.yaml", "002.yaml"]


def test_propose_never_rewrites_an_existing_patch(tmp_path):
    home, run_dir, _ = make_run(tmp_path)
    patches = run_dir / "graph-patches"
    sentinel = "kind: rerun\nfrom_node: alpha\nreason: a\nsource: operator\n"
    (patches / "001.yaml").write_text(sentinel, encoding="utf-8")
    assert gp.propose(str(run_dir), {"kind": "swap_lane", "from_node": "reviewer",
                                     "node": "reviewer", "lane": "fast",
                                     "reason": "b", "source": "operator"}) == 2
    assert (patches / "001.yaml").read_text(encoding="utf-8") == sentinel


@pytest.mark.parametrize("patch", [
    {"kind": "teleport", "from_node": "implementer", "reason": "x", "source": "operator"},
    {"kind": "rerun", "from_node": "implementer", "reason": "x", "source": "operator",
     "sneaky": True},
    {"kind": "rerun", "from_node": "implementer", "source": "operator"},
    {"kind": "rerun", "from_node": "implementer", "reason": "", "source": "operator"},
    {"kind": "rerun", "reason": "x", "source": "operator"},
    {"kind": "rerun", "from_node": "implementer", "reason": "x", "source": "alien"},
    {"kind": "swap_lane", "from_node": "implementer", "node": "reviewer",
     "lane": "fast", "reason": "x", "source": "operator"},
    {"kind": "swap_lane", "from_node": "implementer", "node": "implementer",
     "reason": "x", "source": "operator"},
    {"kind": "insert_before", "from_node": "ghost", "node": "reviewer",
     "new_node": {"name": "gate", "type": "verifier"}, "reason": "x",
     "source": "operator"},
])
def test_propose_rejects_invalid_schema(tmp_path, patch):
    home, run_dir, _ = make_run(tmp_path)
    with pytest.raises(gp.GraphPatchError):
        gp.propose(str(run_dir), patch)
    assert not (run_dir / "graph-patches" / "001.yaml").exists()


def test_propose_stamps_created_at(tmp_path):
    home, run_dir, _ = make_run(tmp_path)
    propose_rerun(run_dir)
    assert load_yaml(run_dir / "graph-patches" / "001.yaml").get("created_at")


# ── materialize ────────────────────────────────────────────────────────────


def test_materialize_rerun_is_unchanged_copy(tmp_path, monkeypatch):
    home, run_dir, workflow = make_run(tmp_path)
    pin_env(monkeypatch, home, workflow)
    propose_rerun(run_dir)
    out = gp.materialize(str(home), "r1", 1)
    assert Path(out).name == "001.workflow.yaml"
    assert load_yaml(out) == load_yaml(workflow)


def test_materialize_swap_lane_changes_one_lane(tmp_path, monkeypatch):
    home, run_dir, workflow = make_run(tmp_path)
    pin_env(monkeypatch, home, workflow)
    gp.propose(str(run_dir), {"kind": "swap_lane", "from_node": "implementer",
                              "node": "implementer", "lane": "fast",
                              "reason": "cheaper", "source": "operator"})
    out = gp.materialize(str(home), "r1", 1)
    after = {node["name"]: node for node in load_yaml(out)["nodes"]}
    before = {node["name"]: node for node in load_yaml(workflow)["nodes"]}
    assert after["implementer"]["model_lane"] == "fast"
    assert after["reviewer"]["model_lane"] == before["reviewer"]["model_lane"]
    assert load_yaml(out)["edges"] == load_yaml(workflow)["edges"]


def test_materialize_insert_before_redirects_edges_and_compiles(tmp_path, monkeypatch):
    home, run_dir, workflow = make_run(tmp_path)
    pin_env(monkeypatch, home, workflow)
    gp.propose(str(run_dir), {
        "kind": "insert_before", "from_node": "gate", "node": "reviewer",
        "new_node": {"name": "gate", "type": "verifier"},
        "reason": "gate before review", "source": "operator"})
    out = gp.materialize(str(home), "r1", 1)
    document = load_yaml(out)
    names = [node["name"] for node in document["nodes"]]
    assert names.index("gate") < names.index("reviewer")
    edges = {(edge["from"], edge["to"]) for edge in document["edges"]}
    assert ("implementer", "gate") in edges
    assert ("implementer", "reviewer") not in edges
    assert ("gate", "reviewer") in edges
    compile_workflow(out)


def test_materialize_rejects_duplicate_new_node_and_leaves_no_file(tmp_path, monkeypatch):
    home, run_dir, workflow = make_run(tmp_path)
    pin_env(monkeypatch, home, workflow)
    gp.propose(str(run_dir), {
        "kind": "insert_before", "from_node": "implementer", "node": "reviewer",
        "new_node": {"name": "implementer", "type": "verifier"},
        "reason": "collision", "source": "operator"})
    with pytest.raises(gp.GraphPatchError):
        gp.materialize(str(home), "r1", 1)
    assert not (run_dir / "graph-patches" / "001.workflow.yaml").exists()


def test_materialize_rejects_compile_break_and_missing_node(tmp_path, monkeypatch):
    home, run_dir, workflow = make_run(tmp_path)
    pin_env(monkeypatch, home, workflow)
    gp.propose(str(run_dir), {"kind": "swap_lane", "from_node": "ghost",
                              "node": "ghost", "lane": "fast",
                              "reason": "missing", "source": "operator"})
    with pytest.raises(gp.GraphPatchError):
        gp.materialize(str(home), "r1", 1)
    assert not (run_dir / "graph-patches" / "001.workflow.yaml").exists()


def test_materialize_replan_grafts_chain_and_compiles(tmp_path, monkeypatch):
    home, run_dir, workflow = make_run(tmp_path)
    pin_env(monkeypatch, home, workflow)
    patch = {"kind": "replan", "from_node": "implementer",
             "reason": "wrong plan", "source": "operator"}
    assert gp.propose(str(run_dir), patch) == 1
    prime_replan(home, "r1", patch, load_yaml(workflow),
                 "impl_probe", "impl_final")

    out = gp.materialize(str(home), "r1", 1)
    document = load_yaml(out)
    assert [node["name"] for node in document["nodes"]] == [
        "planner", "impl_probe", "impl_final", "reviewer"]
    edges = {(edge["from"], edge["to"]) for edge in document["edges"]}
    assert ("planner", "impl_probe") in edges
    assert ("impl_final", "reviewer") in edges
    assert ("impl_probe", "impl_final") in edges
    assert "implementer" not in {endpoint for edge in edges for endpoint in edge}
    assert "implementer" not in {node["name"] for node in document["nodes"]}
    compile_workflow(out)


# ── apply ──────────────────────────────────────────────────────────────────


def apply_patch(home, run_id, n):
    spawn = FakeSpawn()
    result = gp.apply(str(home), run_id, n, engine_root=str(REPO_ROOT), spawn=spawn)
    return spawn, result


def test_apply_spawns_recover_and_records_state(tmp_path, monkeypatch):
    home, run_dir, workflow = make_run(tmp_path)
    pin_env(monkeypatch, home, workflow)
    propose_rerun(run_dir)
    spawn, result = apply_patch(home, "r1", 1)

    assert len(spawn.calls) == 1
    argv = spawn.calls[0]
    assert "mini_ork.recovery.planner" in argv
    assert argv[argv.index("--from-node") + 1] == "implementer"
    assert Path(argv[argv.index("--workflow") + 1]).name == "001.workflow.yaml"
    assert "--force" in argv
    assert result["pid"] == FAKE_PID

    applied = load_json(run_dir / "graph-patches" / "001.applied.json")
    assert applied["pid"] == FAKE_PID
    assert applied["from_node"] == "implementer"
    assert applied["applied_at"]
    assert isinstance(applied["engine_sha"], str)

    repair = load_json(run_dir / "repair.json")
    assert repair["state"] == "reviving"
    assert repair["revive_patch"] == "001"
    assert repair["revive_from_node"] == "implementer"
    assert repair["revived_at"]
    assert repair["kept_marker"] == "keep-me"
    assert repair["attempts"] == [{"from_node": "implementer"}]


def test_apply_refuses_repeat_and_out_of_order(tmp_path, monkeypatch):
    home, run_dir, workflow = make_run(tmp_path)
    pin_env(monkeypatch, home, workflow)
    propose_rerun(run_dir, "implementer")
    propose_rerun(run_dir, "reviewer", "second failure")
    apply_patch(home, "r1", 2)
    with pytest.raises(gp.GraphPatchError):
        apply_patch(home, "r1", 1)
    with pytest.raises(gp.GraphPatchError):
        apply_patch(home, "r1", 2)


def test_apply_dry_run_has_no_side_effects(tmp_path, monkeypatch):
    home, run_dir, workflow = make_run(tmp_path)
    pin_env(monkeypatch, home, workflow)
    propose_rerun(run_dir)
    spawn = FakeSpawn()
    result = gp.apply(str(home), "r1", 1, engine_root=str(REPO_ROOT),
                      spawn=spawn, dry_run=True)
    assert spawn.calls == []
    assert isinstance(result, dict)
    assert not (run_dir / "graph-patches" / "001.applied.json").exists()
    assert (run_dir / "graph-patches" / "001.workflow.yaml").is_file()


def test_apply_replan_spawns_from_chain_head(tmp_path, monkeypatch):
    home, run_dir, workflow = make_run(tmp_path)
    pin_env(monkeypatch, home, workflow)
    patch = {"kind": "replan", "from_node": "implementer",
             "reason": "wrong plan", "source": "operator"}
    gp.propose(str(run_dir), patch)
    prime_replan(home, "r1", patch, load_yaml(workflow),
                 "impl_probe", "impl_final")

    spawn, result = apply_patch(home, "r1", 1)
    argv = spawn.calls[0]
    assert argv[argv.index("--from-node") + 1] == "impl_probe"
    assert result["from_node"] == "impl_probe"

    applied = load_json(run_dir / "graph-patches" / "001.applied.json")
    assert applied["from_node"] == "impl_probe"
    repair = load_json(run_dir / "repair.json")
    assert repair["revive_from_node"] == "impl_probe"


def test_base_workflow_tracks_newest_applied_patch(tmp_path, monkeypatch):
    home, run_dir, workflow = make_run(tmp_path)
    pin_env(monkeypatch, home, workflow)
    propose_rerun(run_dir, "implementer")
    propose_rerun(run_dir, "reviewer", "second failure")
    assert Path(gp.base_workflow(str(home), "r1")).resolve() == workflow.resolve()
    apply_patch(home, "r1", 1)
    assert Path(gp.base_workflow(str(home), "r1")).name == "001.workflow.yaml"
    apply_patch(home, "r1", 2)
    assert Path(gp.base_workflow(str(home), "r1")).name == "002.workflow.yaml"


# ── list / credit ──────────────────────────────────────────────────────────


def test_list_patches_status_and_outcome(tmp_path, monkeypatch):
    home, run_dir, workflow = make_run(tmp_path)
    pin_env(monkeypatch, home, workflow)
    propose_rerun(run_dir, reason="fix the flake")
    gp.propose(str(run_dir), {"kind": "swap_lane", "from_node": "reviewer",
                              "node": "reviewer", "lane": "fast",
                              "reason": "cheaper review", "source": "operator"})
    apply_patch(home, "r1", 1)
    (run_dir / "graph-patches" / "001.outcome.json").write_text(
        json.dumps({"passed": True}), encoding="utf-8")
    entries = {entry["number"]: entry for entry in gp.list_patches(str(home), "r1")}
    assert entries[1]["status"] == "applied"
    assert entries[1]["kind"] == "rerun"
    assert entries[1]["reason"] == "fix the flake"
    assert entries[1]["outcome"] == {"passed": True}
    assert entries[2]["status"] == "proposed"
    assert entries[2]["outcome"] is None


def _credit_fixture(home, spec):
    patches = home / "runs" / "r1" / "graph-patches"
    patches.mkdir(parents=True)
    for n, (kind, passed) in enumerate(spec, start=1):
        document = {"kind": kind, "from_node": f"node_{n}", "reason": "x",
                    "source": "operator", "created_at": "2026-10-22T00:00:00Z"}
        if kind == "swap_lane":
            document.update({"node": f"node_{n}", "lane": "fast"})
        (patches / f"{n:03d}.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        if passed is not None:
            (patches / f"{n:03d}.outcome.json").write_text(
                json.dumps({"passed": passed}), encoding="utf-8")


def test_credit_needs_five_outcomes_and_strict_lead(tmp_path):
    home = tmp_path / "home"
    _credit_fixture(home, [("rerun", True), ("rerun", True), ("rerun", False),
                           ("rerun", False), ("rerun", False)]
                    + [("swap_lane", True)] * 4 + [("swap_lane", False)])
    report = gp.credit(str(home))
    assert report["rerun"]["n"] == 5 and report["rerun"]["passed"] == 2
    assert report["swap_lane"]["n"] == 5 and report["swap_lane"]["passed"] == 4
    assert report["swap_lane"]["credited"] is True


def test_credit_not_credited_when_rerun_control_is_small(tmp_path):
    home = tmp_path / "home"
    _credit_fixture(home, [("rerun", True), ("rerun", True), ("rerun", False)]
                    + [("swap_lane", True)] * 4 + [("swap_lane", False)])
    report = gp.credit(str(home))
    assert report["swap_lane"]["n"] == 5
    assert report["swap_lane"]["credited"] is False


def test_credit_missing_outcomes_count_zero(tmp_path):
    home = tmp_path / "home"
    _credit_fixture(home, [("rerun", None), ("insert_before", None)])
    report = gp.credit(str(home))
    assert report["rerun"]["n"] == 0
    assert report["insert_before"]["n"] == 0


# ── CLI ────────────────────────────────────────────────────────────────────


def run_cli(args, home):
    env = {key: value for key, value in os.environ.items()
           if key not in RUNSCOPED_ENV}
    env.update({"PYTHONPATH": str(REPO_ROOT), "MINI_ORK_HOME": str(home),
                "MINI_ORK_ROOT": str(REPO_ROOT)})
    return subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.graph_patch", *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=120)


def test_cli_propose_and_list_json(tmp_path):
    home = tmp_path / "home"
    (home / "runs" / "r1" / "graph-patches").mkdir(parents=True)
    result = run_cli(["propose", "r1", "--kind", "rerun",
                      "--from-node", "implementer", "--reason", "flake",
                      "--json"], home)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["number"] == 1

    result = run_cli(["list", "r1", "--json"], home)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)[0]["status"] == "proposed"


def test_cli_validate_bad_patch_exits_two(tmp_path):
    home = tmp_path / "home"
    patches = home / "runs" / "r1" / "graph-patches"
    patches.mkdir(parents=True)
    (patches / "001.yaml").write_text(yaml.safe_dump({
        "kind": "swap_lane", "from_node": "ghost", "node": "ghost",
        "lane": "fast", "reason": "not there", "source": "operator",
        "created_at": "2026-10-22T00:00:00Z",
    }, sort_keys=False), encoding="utf-8")
    workflow = home / "workflow.yaml"
    workflow.write_text("nodes:\n  - name: implementer\n    type: implementer\n"
                        "edges: []\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if key not in RUNSCOPED_ENV}
    env.update({"PYTHONPATH": str(REPO_ROOT), "MINI_ORK_HOME": str(home),
                "MINI_ORK_ROOT": str(REPO_ROOT),
                "MINI_ORK_WORKFLOW": str(workflow)})
    result = subprocess.run(
        [sys.executable, "-m", "mini_ork.cli.graph_patch", "validate", "r1", "1"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=120)
    assert result.returncode == 2
    assert (result.stdout + result.stderr).strip()
    assert not (patches / "001.workflow.yaml").exists()


# ── MUTATE wiring ──────────────────────────────────────────────────────────

NODE_ID = "spec_test_author"
NODE_TYPE = "researcher"


def deliver_mutate(monkeypatch, tmp_path, effective_locus, reason="harness fault"):
    run_dir = tmp_path / "runs" / "r1"
    (run_dir / "graph-patches").mkdir(parents=True)
    emitted = []

    def fake_on_node_boundary(ctx, *, spent_usd=0.0):
        return Action(
            kind=runner_orchestrator.MUTATE,
            reason=reason,
            payload={"node_id": NODE_ID, "node_type": NODE_TYPE,
                     "check": "harness_surface",
                     "effective_locus": effective_locus},
        )

    def fake_emit(*args, **kwargs):
        emitted.append({"args": list(args), "kwargs": dict(kwargs)})
        return 0

    monkeypatch.setattr(runner_orchestrator, "on_node_boundary",
                        fake_on_node_boundary)
    monkeypatch.setattr(node_events, "mo_node_emit", fake_emit)
    outcome = ((NODE_ID, NODE_TYPE), 1, "terminal")
    execute._orchestrator_act(str(run_dir), None, "r1", [outcome], {},
                              revision_state={})
    return run_dir, emitted


def test_mutate_verifier_locus_proposes_rerun(tmp_path, monkeypatch):
    run_dir, emitted = deliver_mutate(monkeypatch, tmp_path, "verifier")
    document = load_yaml(run_dir / "graph-patches" / "001.yaml")
    assert document["kind"] == "rerun"
    assert document["from_node"] == NODE_ID
    assert document["source"] == "orchestrator"
    proposals = [json.loads(entry["args"][4]) for entry in emitted
                 if entry["args"][3] == "orchestrator.mutate.proposed"]
    assert proposals[0]["graph_patch"] == 1


def test_mutate_prompt_locus_proposes_replan(tmp_path, monkeypatch):
    run_dir, _ = deliver_mutate(monkeypatch, tmp_path, "prompt")
    document = load_yaml(run_dir / "graph-patches" / "001.yaml")
    assert document["kind"] == "replan"
    assert document["from_node"] == NODE_ID


def test_mutate_propose_failure_never_raises_out(tmp_path, monkeypatch):
    run_dir = tmp_path / "runs" / "r1"
    patches = run_dir / "graph-patches"
    patches.mkdir(parents=True)
    monkeypatch.setattr(runner_orchestrator, "on_node_boundary",
                        lambda ctx, *, spent_usd=0.0: Action(
                            kind=runner_orchestrator.MUTATE, reason="broke",
                            payload={"node_id": NODE_ID, "node_type": NODE_TYPE,
                                     "check": "harness_surface",
                                     "effective_locus": "verifier"}))
    monkeypatch.setattr(node_events, "mo_node_emit", lambda *a, **k: 0)
    patches.chmod(0o500)
    try:
        outcome = ((NODE_ID, NODE_TYPE), 1, "terminal")
        execute._orchestrator_act(str(run_dir), None, "r1", [outcome], {},
                                  revision_state={})
    finally:
        patches.chmod(0o700)
    assert not (patches / "001.yaml").exists()


# ── review additions: control edges stay put; a live run is never patched ──


def test_insert_before_leaves_control_edges_on_the_original_node(tmp_path, monkeypatch):
    home, run_dir, _ = make_run(tmp_path)
    workflow = home / "workflow.yaml"
    workflow.write_text(BASE_WORKFLOW + """\
  - from: reviewer
    to: implementer
    edge_type: retries
""", encoding="utf-8")
    pin_env(monkeypatch, home, workflow)
    gp.propose(str(run_dir), {
        "kind": "insert_before", "from_node": "prep", "node": "implementer",
        "new_node": {"name": "prep", "type": "researcher"},
        "reason": "prep before implementing", "source": "operator"})
    document = load_yaml(gp.materialize(str(home), "r1", 1))
    edges = {(e["from"], e["to"], e.get("edge_type")) for e in document["edges"]}
    assert ("planner", "prep", None) in edges
    assert ("prep", "implementer", "depends_on") in edges
    # The revise loop still targets the implementer, not the inserted node.
    assert ("reviewer", "implementer", "retries") in edges
    assert ("reviewer", "prep", "retries") not in edges


def test_apply_refuses_a_live_run_and_leaves_no_trace(tmp_path, monkeypatch):
    import sqlite3

    home, run_dir, workflow = make_run(tmp_path)
    pin_env(monkeypatch, home, workflow)
    con = sqlite3.connect(str(home / "state.db"))
    con.execute("CREATE TABLE task_runs (id TEXT PRIMARY KEY, status TEXT)")
    con.execute("INSERT INTO task_runs VALUES ('r1', 'executing')")
    con.commit()
    con.close()
    propose_rerun(run_dir)
    spawn = FakeSpawn()
    with pytest.raises(gp.GraphPatchError, match="still executing"):
        gp.apply(str(home), "r1", 1, engine_root=str(REPO_ROOT), spawn=spawn)
    assert spawn.calls == []
    assert not (run_dir / "graph-patches" / "001.applied.json").exists()
    assert load_json(run_dir / "repair.json")["state"] == "repairing"
