"""Unit tests for SDE-6a: ``replan`` graph patches are applicable.

Hermetic: every test builds ``<tmp>/home/runs/<run_id>`` and pins the base
workflow through ``MINI_ORK_WORKFLOW``. The planner lane is replaced by an
injected ``dispatch`` callable, so no test calls a real model or the network.
"""
from __future__ import annotations

import json
import os
import time
import types
from pathlib import Path

import pytest
import yaml

import mini_ork.recovery.graph_patch as gp
from mini_ork.workflow.compiler import compile_workflow

BASE_WORKFLOW = """\
nodes:
  - name: planner
    type: planner
    model_lane: glm
    prompt_ref: prompts/planner.md
  - name: implementer
    type: implementer
    model_lane: sonnet
    prompt_ref: prompts/implementer.md
    description: carries the edit
  - name: reviewer
    type: reviewer
    model_lane: glm
    prompt_ref: prompts/reviewer.md
  - name: sentry
    type: reflector
    model_lane: glm
    prompt_ref: prompts/sentry.md
edges:
  - from: planner
    to: implementer
    edge_type: depends_on
  - from: implementer
    to: reviewer
    edge_type: depends_on
  - from: reviewer
    to: implementer
    edge_type: retries
    max_rounds: 2
  - from: sentry
    to: implementer
    edge_type: escalates_to
"""

FAILING = "implementer"
REASON = "implementer wedged after the probe cutoff"


class FakeReply:
    def __init__(self, text: str, ok: bool = True, error: str = ""):
        self.ok = ok
        self.text = text
        self.error = error
        self.cost_usd = 0.0
        self.model = "fake"


class FakeDispatch:
    """``callable(request) -> result`` seam that records every request."""

    def __init__(self, text: str | None = None, error: Exception | None = None):
        self.requests = []
        self.cwd_was_dir = []
        self._text = text
        self._error = error

    def __call__(self, request):
        self.requests.append(request)
        cwd = getattr(request, "cwd", None)
        self.cwd_was_dir.append(Path(cwd).is_dir() if cwd else None)
        if self._error is not None:
            raise self._error
        assert self._text is not None
        return FakeReply(self._text)

    @property
    def prompt(self) -> str:
        return str(self.requests[0].prompt)


def chain_nodes(*names: str) -> list[dict]:
    return [{"name": name, "type": "implementer", "model_lane": "sonnet",
             "prompt_ref": "prompts/implementer.md",
             "description": "carries the edit"} for name in names]


def good_dispatch(*names: str) -> FakeDispatch:
    names = names or ("impl_probe", "impl_final")
    return FakeDispatch(json.dumps({"nodes": chain_nodes(*names)}))


@pytest.fixture
def world(tmp_path, monkeypatch) -> types.SimpleNamespace:
    home = tmp_path / "home"
    run_id = "replan-run"
    run_dir = home / "runs" / run_id
    patch_dir = run_dir / "graph-patches"
    patch_dir.mkdir(parents=True)
    workflow = tmp_path / "base-workflow.yaml"
    workflow.write_text(BASE_WORKFLOW, encoding="utf-8")
    for key in ("MO_REPLAN_LANE", "MINI_ORK_WORKFLOW", "MINI_ORK_RUN_DIR",
                "MINI_ORK_DB", "MINI_ORK_RECIPE"):
        monkeypatch.delenv(key, raising=False)
    patch = {"kind": "replan", "from_node": FAILING, "reason": REASON,
             "source": "orchestrator"}
    number = gp.propose(str(run_dir), patch)
    return types.SimpleNamespace(
        home=home, run_id=run_id, run_dir=run_dir, patch_dir=patch_dir,
        workflow=workflow, patch=patch, number=number,
        base=yaml.safe_load(BASE_WORKFLOW))


def chain(world, dispatch):
    return gp._replan_chain(str(world.home), world.run_id, world.patch,
                            world.base, dispatch=dispatch)


def prime_and_materialize(world, monkeypatch, *names):
    accepted = chain(world, good_dispatch(*names))
    monkeypatch.setenv("MINI_ORK_WORKFLOW", str(world.workflow))
    return accepted, Path(gp.materialize(str(world.home), world.run_id,
                                         world.number))


def edges_of(document):
    return {(str(edge.get("from")), str(edge.get("to")),
             str(edge.get("edge_type") or "depends_on"))
            for edge in (document.get("edges") or []) if isinstance(edge, dict)}


# ── the constant / the old refusal ─────────────────────────────────────────


def test_replan_supported_is_true():
    assert gp.REPLAN_SUPPORTED is True
    assert "replan patches are not applicable yet" not in Path(gp.__file__).read_text(
        encoding="utf-8")


def test_replan_patch_materializes(world, monkeypatch):
    _accepted, out = prime_and_materialize(world, monkeypatch)
    assert out.is_file()
    assert out.name == f"{world.number:03d}.workflow.yaml"


# ── the graft ──────────────────────────────────────────────────────────────


def test_two_node_chain_replaces_failing_node_and_rewires_edges(world, monkeypatch):
    accepted, out = prime_and_materialize(world, monkeypatch,
                                          "impl_probe", "impl_final")
    assert [node["name"] for node in accepted] == ["impl_probe", "impl_final"]

    document = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert [node["name"] for node in document["nodes"]] == [
        "planner", "impl_probe", "impl_final", "reviewer", "sentry"]
    for node in document["nodes"][1:3]:
        assert (node["type"], node["model_lane"], node["prompt_ref"]) == (
            "implementer", "sonnet", "prompts/implementer.md")

    edges = edges_of(document)
    assert ("planner", "impl_probe", "depends_on") in edges       # incoming dataflow
    assert ("impl_final", "reviewer", "depends_on") in edges      # outgoing dataflow
    assert ("impl_probe", "impl_final", "depends_on") in edges    # chain link
    assert ("reviewer", "impl_final", "retries") in edges         # control edge
    assert ("sentry", "impl_final", "escalates_to") in edges      # control edge
    assert FAILING not in {node["name"] for node in document["nodes"]}
    assert FAILING not in {src for src, _to, _kind in edges} | {
        to for _src, to, _kind in edges}

    compiled = compile_workflow(out)
    assert set(compiled.nodes) == {
        "planner", "impl_probe", "impl_final", "reviewer", "sentry"}


def test_one_node_chain_accepted(world):
    assert [n["name"] for n in chain(world, good_dispatch("impl_solo"))] == [
        "impl_solo"]


def test_three_node_chain_accepted(world):
    assert [n["name"] for n in chain(
        world, good_dispatch("impl_a", "impl_b", "impl_c"))] == [
        "impl_a", "impl_b", "impl_c"]


# ── cache determinism ──────────────────────────────────────────────────────


def test_chain_cached_and_second_materialize_does_not_dispatch(world, monkeypatch):
    accepted, out = prime_and_materialize(world, monkeypatch)
    first_bytes = out.read_bytes()
    patch_bytes = (world.patch_dir / f"{world.number:03d}.yaml").read_bytes()

    cache_files = list(world.patch_dir.glob("*.replan.json"))
    assert len(cache_files) == 1
    stored = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert stored == {"nodes": accepted}

    # A cache hit must not dispatch: replace the provider seam with a bomb.
    import mini_ork.dispatch.providers as providers

    def _bomb(_request):
        raise AssertionError("cached replan must not dispatch again")

    monkeypatch.setattr(providers, "dispatch_model", _bomb, raising=False)
    out_again = gp.materialize(str(world.home), world.run_id, world.number)
    assert Path(out_again).read_bytes() == first_bytes
    assert (world.patch_dir / f"{world.number:03d}.yaml").read_bytes() == patch_bytes


# ── rejected replies ───────────────────────────────────────────────────────


def _assert_rejected(world, dispatch):
    with pytest.raises(gp.GraphPatchError) as excinfo:
        chain(world, dispatch)
    assert str(excinfo.value).strip()
    assert list(world.patch_dir.glob("*.replan.json")) == []


def test_reject_non_json_reply(world):
    _assert_rejected(world, FakeDispatch("sure, two nodes ought to fix it"))


def test_reject_zero_nodes(world):
    _assert_rejected(world, FakeDispatch(json.dumps({"nodes": []})))


def test_reject_four_nodes(world):
    _assert_rejected(world, good_dispatch("a", "b", "c", "d"))


def test_reject_foreign_template_triple(world):
    nodes = chain_nodes("impl_probe", "impl_final")
    for node in nodes:
        node["prompt_ref"] = "prompts/ghost.md"
    _assert_rejected(world, FakeDispatch(json.dumps({"nodes": nodes})))


def test_reject_existing_node_name(world):
    _assert_rejected(world, FakeDispatch(
        json.dumps({"nodes": chain_nodes("impl_probe", "reviewer")})))


def test_reject_duplicate_chain_names(world):
    _assert_rejected(world, FakeDispatch(
        json.dumps({"nodes": chain_nodes("impl_twice", "impl_twice")})))


def test_reject_dispatch_error(world):
    _assert_rejected(world, FakeDispatch(error=RuntimeError("lane on fire")))


def test_reject_not_ok_dispatch(world):
    class _NotOk(FakeDispatch):
        def __call__(self, request):
            self.requests.append(request)
            return FakeReply("", ok=False, error="provider exploded")

    _assert_rejected(world, _NotOk())


# ── apply ──────────────────────────────────────────────────────────────────


def test_apply_revives_from_chain_head(world, tmp_path, monkeypatch):
    accepted = chain(world, good_dispatch("impl_probe", "impl_final"))
    monkeypatch.setenv("MINI_ORK_WORKFLOW", str(world.workflow))
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "no-such.db"))

    captured = {}

    def spawn(argv):
        captured["argv"] = list(argv)
        return 4242

    result = gp.apply(str(world.home), world.run_id, world.number,
                      engine_root=str(tmp_path), spawn=spawn)

    argv = captured["argv"]
    assert argv[argv.index("--from-node") + 1] == accepted[0]["name"] == "impl_probe"
    assert argv[argv.index("--workflow") + 1] == result["workflow"]

    applied = json.loads(
        (world.patch_dir / f"{world.number:03d}.applied.json").read_text("utf-8"))
    assert applied["from_node"] == "impl_probe"
    repair = json.loads((world.run_dir / "repair.json").read_text("utf-8"))
    assert repair["revive_from_node"] == "impl_probe"


# ── lane resolution ────────────────────────────────────────────────────────


def test_lane_env_wins(world, monkeypatch):
    (world.run_dir / "config").mkdir()
    (world.run_dir / "config" / "agents.yaml").write_text(
        "lanes:\n  planner: plan-from-agents\n", encoding="utf-8")
    monkeypatch.setenv("MO_REPLAN_LANE", "replan-fast-lane")
    dispatch = good_dispatch()
    chain(world, dispatch)
    assert str(dispatch.requests[0].model) == "replan-fast-lane"


def test_lane_run_agents_yaml_planner(world):
    (world.run_dir / "config").mkdir()
    (world.run_dir / "config" / "agents.yaml").write_text(
        "lanes:\n  planner: plan-from-agents\n", encoding="utf-8")
    dispatch = good_dispatch()
    chain(world, dispatch)
    assert str(dispatch.requests[0].model) == "plan-from-agents"


def test_lane_defaults_to_glm(world):
    dispatch = good_dispatch()
    chain(world, dispatch)
    assert str(dispatch.requests[0].model) == "glm"


# ── the dispatch request ───────────────────────────────────────────────────


def test_dispatch_request_is_one_bounded_scratch_call(world):
    dispatch = good_dispatch()
    chain(world, dispatch)
    assert len(dispatch.requests) == 1
    request = dispatch.requests[0]
    assert str(request.prompt).strip()
    assert float(request.timeout_s) > 0
    assert isinstance(request.max_turns, int) and request.max_turns >= 1
    assert dispatch.cwd_was_dir == [True], "cwd must be a scratch dir at call time"
    assert str(request.cwd) != str(Path.cwd())
    assert not str(request.cwd).startswith(str(world.run_dir))


def test_prompt_carries_workflow_reason_and_evidence(world):
    (world.run_dir / "repair.json").write_text(json.dumps({
        "attempts": [
            {"action": "reverify", "reason": "EARLY-ATTTEMPT"},
            {"action": "revise", "reason": "REPAIR-LAST",
             "diagnosis": {"summary": "REPAIR-DIAG"}},
        ],
    }), encoding="utf-8")
    (world.run_dir / "diagnosis.json").write_text(
        json.dumps({"summary": "DXJSON"}), encoding="utf-8")
    (world.run_dir / "older-implementer.log").write_text(
        "OLDLOG\n" * 3, encoding="utf-8")
    (world.run_dir / "newest-implementer.log").write_text(
        "\n".join(f"NLOG-{i:03d}" for i in range(80)) + "\n", encoding="utf-8")
    (world.run_dir / "execute.log").write_text(
        "EXECLOG-nomatch\n" * 3, encoding="utf-8")
    now = time.time()
    os.utime(world.run_dir / "older-implementer.log", (now - 30, now - 30))
    os.utime(world.run_dir / "newest-implementer.log", (now - 10, now - 10))
    os.utime(world.run_dir / "execute.log", (now, now))

    dispatch = good_dispatch()
    chain(world, dispatch)
    prompt = dispatch.prompt

    assert "prompts/planner.md" in prompt          # base workflow nodes
    assert "max_rounds" in prompt                  # base workflow edges
    assert FAILING in prompt and REASON in prompt  # failing node + reason
    assert "REPAIR-LAST" in prompt and "REPAIR-DIAG" in prompt
    assert "DXJSON" in prompt
    assert "NLOG-020" in prompt and "NLOG-079" in prompt   # last 60 of 80
    assert "NLOG-019" not in prompt
    assert "OLDLOG" not in prompt                  # stale matching log
    assert "EXECLOG-nomatch" not in prompt         # newer, name does not match


def test_whole_reply_code_fence_is_unwrapped(world):
    # Review addition: a model that wraps the JSON in a ```json fence despite
    # the instruction must not fail the patch; prose around a fence still does.
    body = json.dumps({"nodes": chain_nodes("impl_fenced")})
    assert [n["name"] for n in chain(
        world, FakeDispatch(f"```json\n{body}\n```"))] == ["impl_fenced"]


def test_prose_around_a_fence_is_still_rejected(world):
    body = json.dumps({"nodes": chain_nodes("impl_fenced")})
    _assert_rejected(world, FakeDispatch(f"Here you go:\n```json\n{body}\n```"))
