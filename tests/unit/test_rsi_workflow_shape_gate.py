"""recursive-self-improve: no prose node may sit on a lane that arms a reply-shape gate.

2026-10-23 (self-improve-iter-1-20261023054815): bottleneck_lens is a researcher
that writes lens-bottleneck.md, but its model_lane was `planner`. The executor
passes the lane in llm_dispatch's --node-type slot, so the planner JSON gate
rejected every markdown reply and re-ran the chain: 4 lanes, $4.84, 21.8 min.
"""
from pathlib import Path

import pytest
import yaml

from mini_ork.dispatch import llm_dispatch as L
from mini_ork.dispatch.predicates import SHAPE_REJECT_RC

WORKFLOW = (Path(__file__).resolve().parents[2]
            / "recipes" / "recursive-self-improve" / "workflow.yaml")


def _nodes():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["nodes"]


def _prose_nodes():
    return [n for n in _nodes()
            if n.get("model_lane") and n["type"] not in L._NODE_SHAPE_PREDICATES]


def test_workflow_has_laned_prose_nodes():
    assert {"bottleneck_lens", "perf_lens", "opus_synthesizer"} <= {
        n["name"] for n in _prose_nodes()}


@pytest.mark.parametrize("node", _prose_nodes(), ids=lambda n: n["name"])
def test_prose_node_lane_does_not_arm_shape_gate(node, monkeypatch):
    monkeypatch.delenv("MO_SHAPE_CHECK", raising=False)
    monkeypatch.delenv("MO_PLAN_ARTIFACT", raising=False)
    assert L._shape_predicate_for(node["model_lane"]) is None, (
        f"{node['name']} ({node['type']}) is on lane {node['model_lane']!r}, "
        "which arms the planner's JSON reply gate")


def test_bottleneck_scan_is_dispatched_once_on_a_markdown_reply(monkeypatch, tmp_path):
    # Same isolation as tests/unit/test_llm_dispatch_shape_fallback.py::_isolate.
    monkeypatch.setattr(L, "cost_circuit_open", lambda *a, **k: False)
    monkeypatch.setattr(L, "write_llm_calls_row", lambda *a, **k: None)
    monkeypatch.setattr(L, "backoff_seconds", lambda n: 0)
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("MO_DISPATCH_MAX_ATTEMPTS", "3")
    for key in ("MO_SHAPE_CHECK", "MO_FUSE_ENABLED", "MO_PLAN_ARTIFACT"):
        monkeypatch.delenv(key, raising=False)
    lane = next(n["model_lane"] for n in _nodes() if n["name"] == "bottleneck_lens")
    calls = []

    def lane_writes_markdown(model, prompt, out_file, timeout_s, max_turns):
        calls.append(model)
        Path(out_file).write_text("# Bottleneck Scan\n\nmarkdown, not json\n")
        return SHAPE_REJECT_RC

    out = str(tmp_path / "out.txt")
    L.llm_dispatch(["--node-type", lane, "--model", lane, "--prompt-text", "x",
                    "--out", out], dispatch_fn=lane_writes_markdown)
    # One lane run. On lane `planner` the shape-reject retry re-runs it 3x.
    assert calls == [lane]
