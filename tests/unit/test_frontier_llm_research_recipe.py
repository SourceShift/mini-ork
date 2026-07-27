"""Regression tests for the 200-paper LibWit research recipe."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from mini_ork.workflow.compiler import compile_workflow


ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "recipes" / "frontier-llm-research"


def _pipeline():
    spec = importlib.util.spec_from_file_location(
        "frontier_research_pipeline", RECIPE / "lib" / "research_pipeline.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(rank: int) -> dict[str, object]:
    arxiv_id = f"2607.{rank:05d}"
    return {
        "source_id": f"arxiv:{arxiv_id}",
        "rank": rank,
        "title": f"Frontier inference paper {rank}",
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "published_at": "2026-07-01",
        "retrieved_at": "2026-07-26T00:00:00Z",
        "topics": ["inference-time compute"],
        "abstract": f"Abstract {rank}",
    }


def test_workflow_has_one_bounded_artifact_handoff_per_summary_shard():
    compiled = compile_workflow(RECIPE / "workflow.yaml")
    summary_nodes = [name for name in compiled.nodes if name.startswith("summarize_shard_")]

    assert len(summary_nodes) == 10
    for index, node_name in enumerate(summary_nodes, start=1):
        bindings = compiled.bindings_for(node_name)
        assert len(bindings) == 1
        assert bindings[0].producer_node == "corpus_sharder"
        assert bindings[0].producer_output == f"source_shard_{index:02d}"
        assert bindings[0].consumer_input == "source_shard"

    rollup_bindings = compiled.bindings_for("technique_rollup")
    assert len(rollup_bindings) == 10
    assert all(binding.consumer_input == "source_summaries" for binding in rollup_bindings)


def test_deterministic_verifiers_emit_nonvacuous_pass_envelopes():
    scripts = (
        "collect-latest-libwit.py",
        "shard-corpus.py",
        "prepare-technique-rollup.py",
        "assemble-aggregation.py",
        "verify-aggregation.py",
    )

    for script_name in scripts:
        content = (RECIPE / "verifiers" / script_name).read_text(encoding="utf-8")
        assert '"pass":true' in content, script_name


def test_collector_identifies_the_mini_ork_client_to_libwit(monkeypatch, tmp_path: Path):
    pipeline = _pipeline()
    plan_path = tmp_path / "collection-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "required_source_count": 1,
                "results_per_query": 1,
                "max_workers": 1,
                "queries": [{"topic": "test", "query": "test query"}],
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "results": [
                        {
                            "results": [
                                {
                                    "paper_uid": "arxiv:2607.00001",
                                    "arxiv_id": "2607.00001",
                                    "title": "Test paper",
                                    "published": "2026-07-01",
                                    "abstract": "Test abstract",
                                }
                            ]
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["user_agent"] = request.get_header("User-agent")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setenv("ARXIV_API_TOKEN", "test-token")
    monkeypatch.setattr(pipeline, "urlopen", fake_urlopen)

    pipeline.collect(plan_path, tmp_path / "source-corpus.json")

    assert captured["user_agent"].startswith("mini-ork/")
    assert captured["timeout"] == 45.0


def test_200_source_fixture_survives_sharding_rollup_and_assembly(tmp_path: Path):
    pipeline = _pipeline()
    corpus_path = tmp_path / "source-corpus.json"
    corpus_path.write_text(
        json.dumps({"source_count": 200, "required_source_count": 200, "sources": [_source(i) for i in range(1, 201)]}),
        encoding="utf-8",
    )
    shard_paths = [tmp_path / "shards" / f"source-shard-{index:02d}.json" for index in range(1, 11)]

    pipeline.shard(corpus_path, shard_paths)

    summary_paths: list[Path] = []
    for shard_path in shard_paths:
        shard = json.loads(shard_path.read_text(encoding="utf-8"))
        summaries = [
            {
                **source,
                "summary_paragraph": f"Evidence-bound summary for {source['source_id']}.",
                "prompt_instructions": ["State the objective and expected output."],
            }
            for source in shard["sources"]
        ]
        summary_path = tmp_path / "summaries" / f"shard-{shard['shard_id']}.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(
                {
                    "shard_id": shard["shard_id"],
                    "summaries": summaries,
                    "shard_techniques": [
                        {
                            "technique": "State the objective",
                            "guidance": "State the objective and expected output.",
                            "source_ids": [summary["source_id"] for summary in summaries],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        summary_paths.append(summary_path)

    rollup_path = tmp_path / "technique-rollup.json"
    pipeline.rollup(summary_paths, rollup_path)
    rollup = json.loads(rollup_path.read_text(encoding="utf-8"))
    assert rollup["summary_count"] == 200
    assert len(rollup["source_index"]) == 200

    techniques_path = tmp_path / "unified-techniques.md"
    techniques_path.write_text("- State the objective. (arxiv:2607.00001)\n", encoding="utf-8")
    aggregation_path = tmp_path / "aggregation.md"
    pipeline.assemble(summary_paths, techniques_path, aggregation_path)
    pipeline.verify(aggregation_path)

    aggregation = aggregation_path.read_text(encoding="utf-8")
    assert aggregation.count("\n### ") == 200
    assert aggregation.count("How to write a proper prompt:") == 200
