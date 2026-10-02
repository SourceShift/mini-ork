"""Structural tests for the coord-technique-review recipe.

The deterministic stages are shared with rsi-technique-review (covered by
test_rsi_technique_review_recipe.py); this file pins what the coordination
variant changes: the three-lane rotation, the three-reviewer merge, the
paper digest stage, and the verifier wiring to the shared library.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from mini_ork.workflow.compiler import compile_workflow


ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "recipes" / "coord-technique-review"


def test_workflow_rotates_three_lanes_and_three_reviewers():
    compiled = compile_workflow(RECIPE / "workflow.yaml")
    raw = yaml.safe_load((RECIPE / "workflow.yaml").read_text())
    llm = [n for n in raw["nodes"] if n["type"] == "researcher"]
    assert {n["model_lane"] for n in llm} == {"glm_lens", "minimax_lens", "deepseek_lens"}
    shard_lanes = [n["model_lane"] for n in llm if n["name"].startswith("extract_shard_")]
    assert len(shard_lanes) == 10 and len(set(shard_lanes)) == 3
    assert len(compiled.bindings_for("technique_catalog")) == 11  # 10 shards + corpus
    assert len(compiled.bindings_for("review_merger")) == 4  # 3 reviews + pack
    assert len(compiled.bindings_for("paper_digest")) == 1


def test_verifiers_point_at_existing_shared_library():
    raw = yaml.safe_load((RECIPE / "workflow.yaml").read_text())
    for node in raw["nodes"]:
        if node["type"] == "verifier":
            assert (RECIPE / node["verifier_ref"]).is_file(), node["verifier_ref"]
    os.environ.setdefault("MINI_ORK_RUN_DIR", "/tmp")
    spec = importlib.util.spec_from_file_location("coord_stage", RECIPE / "verifiers" / "_stage.py")
    assert spec and spec.loader
    _stage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_stage)
    assert Path(_stage.REVIEW_LIB).is_file()
    assert Path(_stage.FRONTIER_LIB).is_file()


def test_collection_plan_is_2026_and_sized_for_ten_shards():
    plan = json.loads((RECIPE / "collection-plan.json").read_text())
    assert plan["date_from"] >= "2026-01-01"
    assert plan["required_source_count"] % 10 == 0
    topics = [q["topic"] for q in plan["queries"]]
    assert len(topics) == len(set(topics))


def test_paper_digest_renders_relevance_tiers(tmp_path: Path):
    run_dir = tmp_path / "run"
    inputs = tmp_path / "inputs" / "paper_index"
    inputs.mkdir(parents=True)
    run_dir.mkdir()
    papers = {
        "arxiv:2606.00001": {"source_id": "arxiv:2606.00001", "title": "CoAgent", "url": "u1", "rank": 2,
                             "summary": "s1", "rsi_relevance": "high",
                             "techniques": [{"name": "lease", "mechanism": "m", "result": "r"}]},
        "arxiv:2606.00002": {"source_id": "arxiv:2606.00002", "title": "Other", "url": "u2", "rank": 1,
                             "summary": "s2", "rsi_relevance": "low", "techniques": []},
    }
    (inputs / "paper-index.json").write_text(json.dumps({"paper_count": 2, "papers": papers}))
    env = {**os.environ, "MINI_ORK_RUN_DIR": str(run_dir), "MINI_ORK_NODE_INPUT_DIR": str(tmp_path / "inputs")}
    subprocess.run([sys.executable, str(RECIPE / "verifiers" / "digest.py")], env=env, check=True, capture_output=True)
    digest = (run_dir / "paper-digest.md").read_text()
    assert digest.index("## Relevance: high") < digest.index("## Relevance: low")
    assert "**lease**: m — r" in digest
