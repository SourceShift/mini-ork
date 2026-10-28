"""Gap N1: the harness records and stamps the lane that ACTUALLY served a node.

Two halves:

* ``llm_dispatch`` writes ``<run_dir>/.lane-<MO_NODE_ID>.json`` from the
  dispatch's own ``selected_model`` — the per-node evidence the run-wide
  ``.last-llm-lane`` cannot provide (parallel lenses share one run dir);
* ``_stamp_served_lane`` copies that record over the LLM's self-reported
  ``lane`` in the node's JSON artifact (or writes ``<out_file>.lane.json`` for
  a markdown-only node), so a reader of the artifacts can tell which model
  judged.

Hermetic: a fake ``dispatch_fn`` is injected through the entrypoint's seam, the
state DB points at a path that does not exist, and inherited ``MINI_ORK_*``/
``MO_*`` steering is scrubbed.
"""
from __future__ import annotations

import json
import os

import pytest

from mini_ork.verify.panel_heterogeneity import lane_family


def _empty_providers(tmp_path):
    providers = tmp_path / "providers-empty.yaml"
    providers.write_text("providers: {}\n", encoding="utf-8")
    return providers


def _scrub(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("MINI_ORK_", "MO_")):
            monkeypatch.delenv(key, raising=False)


# ── mechanism 1: the per-node lane record ───────────────────────────────────

def _fake_dispatch(served_lane):
    def dispatch(model, prompt, out_file, timeout_s, max_turns):
        with open(out_file, "w", encoding="utf-8") as handle:
            handle.write("judge reply\n")
        with open(out_file + ".model", "w", encoding="utf-8") as handle:
            handle.write(served_lane)
        return 0
    return dispatch


def _dispatch(tmp_path, monkeypatch, chain, node_id, served):
    from mini_ork.dispatch.llm_dispatch import llm_dispatch

    _scrub(monkeypatch)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("MINI_ORK_RUN_DIR", str(run_dir))
    monkeypatch.setenv("MINI_ORK_DB", str(tmp_path / "absent-state.db"))
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(_empty_providers(tmp_path)))
    monkeypatch.setenv("MO_LANE_AFFINITY", "0")
    if node_id is not None:
        monkeypatch.setenv("MO_NODE_ID", node_id)
    out_file = tmp_path / "out.md"
    rc = llm_dispatch(
        ["--node-type", "researcher", "--prompt-text", "judge",
         "--out", str(out_file), "--model", chain],
        dispatch_fn=_fake_dispatch(served))
    return rc, run_dir


def _per_node_records(run_dir):
    return sorted(p for p in run_dir.rglob(".lane-*.json")
                  if p.name != ".lane-affinity.json")


def test_dispatch_records_requested_served_and_fallback(tmp_path, monkeypatch):
    rc, run_dir = _dispatch(tmp_path, monkeypatch, "opus,glm", "opus_judge", "glm")
    assert rc == 0
    record = json.loads((run_dir / ".lane-opus_judge.json").read_text(encoding="utf-8"))
    assert record["requested"] == "opus"
    assert record["served"] == "glm"
    assert record["fallback"] is True
    assert isinstance(record["at"], (int, float))


def test_dispatch_lead_lane_is_not_a_fallback(tmp_path, monkeypatch):
    rc, run_dir = _dispatch(tmp_path, monkeypatch, "opus,glm", "glm_judge", "opus")
    assert rc == 0
    record = json.loads((run_dir / ".lane-glm_judge.json").read_text(encoding="utf-8"))
    assert record["requested"] == "opus"
    assert record["served"] == "opus"
    assert record["fallback"] is False


def test_dispatch_skips_unsafe_node_id(tmp_path, monkeypatch):
    rc, run_dir = _dispatch(tmp_path, monkeypatch, "opus,glm", "ju/dge", "glm")
    assert rc == 0, "an unsafe node id must never change a dispatch outcome"
    assert _per_node_records(run_dir) == []


def test_dispatch_without_node_id_writes_nothing(tmp_path, monkeypatch):
    rc, run_dir = _dispatch(tmp_path, monkeypatch, "opus,glm", None, "glm")
    assert rc == 0
    assert _per_node_records(run_dir) == []


# ── mechanism 2: stamping the artifact ──────────────────────────────────────

@pytest.fixture
def stamp(monkeypatch, tmp_path):
    _scrub(monkeypatch)
    monkeypatch.setenv("MINI_ORK_PROVIDERS", str(_empty_providers(tmp_path)))
    from mini_ork.cli.execute_handlers import _stamp_served_lane
    return _stamp_served_lane


def _plant_record(run_dir, node, requested="opus", served="glm"):
    (run_dir / f".lane-{node}.json").write_text(json.dumps({
        "requested": requested, "served": served,
        "fallback": served != requested, "at": 1793000000}), encoding="utf-8")


def test_stamp_overwrites_self_reported_lane(tmp_path, stamp):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _plant_record(run_dir, "opus_judge")
    context = run_dir / "context-opus_judge.json"
    context.write_text(json.dumps({
        "lane": "opus", "model": "opus", "served_lane": "opus",
        "verdict": "confirmed", "summary": "judge prose"}), encoding="utf-8")
    out_file = run_dir / "judge-opus-audit.md"
    out_file.write_text("# judge report\n", encoding="utf-8")

    stamp(str(run_dir), "opus_judge", str(out_file))

    stamped = json.loads(context.read_text(encoding="utf-8"))
    assert stamped["served_lane"] == "glm"
    assert stamped["requested_lane"] == "opus"
    assert stamped["served_family"] == lane_family("glm")
    assert stamped["lane"] == "opus", "the LLM's own label is left alone"
    assert stamped["verdict"] == "confirmed"
    assert stamped["summary"] == "judge prose"


def test_stamp_writes_this_nodes_lens_and_leaves_a_sibling_lens(tmp_path, stamp):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _plant_record(run_dir, "code_impact_lens")
    mine = run_dir / "lens-code_impact.json"
    mine.write_text(json.dumps({"lens": "code_impact", "served_lane": "opus"}),
                    encoding="utf-8")
    sibling = run_dir / "lens-prior_art.json"
    sibling.write_text(json.dumps({"lens": "prior_art", "served_lane": "opus"}),
                       encoding="utf-8")
    out_file = run_dir / "lens-code_impact.md"
    out_file.write_text("# lens output\n", encoding="utf-8")

    stamp(str(run_dir), "code_impact_lens", str(out_file))

    stamped = json.loads(mine.read_text(encoding="utf-8"))
    assert stamped["served_lane"] == "glm"
    assert stamped["requested_lane"] == "opus"
    assert stamped["served_family"] == lane_family("glm")
    assert stamped["lens"] == "code_impact"
    assert json.loads(sibling.read_text(encoding="utf-8")) == {
        "lens": "prior_art", "served_lane": "opus"}


def test_markdown_only_output_gets_a_lane_sidecar(tmp_path, stamp):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _plant_record(run_dir, "opus_judge")
    out_file = run_dir / "judge-opus-audit.md"
    out_file.write_text("# markdown only\n", encoding="utf-8")

    stamp(str(run_dir), "opus_judge", str(out_file))

    sidecar = json.loads((run_dir / "judge-opus-audit.md.lane.json")
                         .read_text(encoding="utf-8"))
    assert sidecar == {"served_lane": "glm", "requested_lane": "opus",
                       "served_family": lane_family("glm")}


def test_json_artifact_suppresses_the_markdown_sidecar(tmp_path, stamp):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _plant_record(run_dir, "opus_judge")
    (run_dir / "context-opus_judge.json").write_text(
        json.dumps({"lane": "opus", "verdict": "confirmed"}), encoding="utf-8")
    out_file = run_dir / "judge-opus-audit.md"
    out_file.write_text("# judge report\n", encoding="utf-8")

    stamp(str(run_dir), "opus_judge", str(out_file))

    assert not (run_dir / "judge-opus-audit.md.lane.json").exists()


@pytest.mark.parametrize("lane_record", [None, "{not json"])
def test_missing_or_unreadable_lane_record_stamps_nothing(tmp_path, stamp,
                                                          lane_record):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    if lane_record is not None:
        (run_dir / ".lane-opus_judge.json").write_text(lane_record, encoding="utf-8")
    context = run_dir / "context-opus_judge.json"
    before = json.dumps({"lane": "opus", "verdict": "confirmed"}, indent=2)
    context.write_text(before, encoding="utf-8")
    out_file = run_dir / "judge-opus-audit.md"
    out_file.write_text("# judge report\n", encoding="utf-8")

    stamp(str(run_dir), "opus_judge", str(out_file))

    assert context.read_text(encoding="utf-8") == before
    assert not (run_dir / "judge-opus-audit.md.lane.json").exists()


@pytest.mark.parametrize("payload", ['["not", "an", "object"]',
                                     '{"unterminated": '])
def test_non_object_or_corrupt_artifact_never_raises(tmp_path, stamp, payload):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _plant_record(run_dir, "opus_judge")
    context = run_dir / "context-opus_judge.json"
    context.write_text(payload, encoding="utf-8")
    out_file = run_dir / "judge-opus-audit.md"
    out_file.write_text("# judge report\n", encoding="utf-8")

    stamp(str(run_dir), "opus_judge", str(out_file))

    assert context.read_text(encoding="utf-8") == payload
