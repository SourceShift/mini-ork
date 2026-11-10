"""Regression tests for the 200-paper LibWit research recipe."""

from __future__ import annotations

import importlib.util
import json
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

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
    captured: dict[str, Any] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return 200

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
    # Keep this test on the search call alone: body-text enrichment has its own
    # tests below and uses a different (longer) timeout.
    monkeypatch.setenv("MINI_ORK_RESEARCH_FULLTEXT", "0")
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


def _collect_fixture(tmp_path: Path) -> tuple[Path, Path]:
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
    return plan_path, tmp_path / "source-corpus.json"


def _install_libwit(monkeypatch, pipeline, chunk_payload: object) -> None:
    class Response:
        def __init__(self, payload: object):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return 200

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    search_payload = {
        "data": [
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

    def fake_urlopen(request, timeout):  # noqa: ARG001 - signature matches urlopen
        if "/search/batch" in request.full_url:
            return Response(search_payload)
        if "/chunks" in request.full_url:
            if chunk_payload is None:
                raise HTTPError(request.full_url, 404, "not found", Message(), None)
            return Response(chunk_payload)
        raise HTTPError(request.full_url, 404, "not found", Message(), None)

    monkeypatch.setenv("ARXIV_API_TOKEN", "test-token")
    monkeypatch.setenv("MINI_ORK_RESEARCH_FULLTEXT_WORKERS", "1")
    monkeypatch.setattr(pipeline, "urlopen", fake_urlopen)


def test_collector_reads_body_text_and_writes_a_evidence_file(monkeypatch, tmp_path: Path):
    pipeline = _pipeline()
    _install_libwit(
        monkeypatch,
        pipeline,
        {
            "chunks": [
                {"index": 0, "section": "Introduction", "content": "Body introduction text."},
                {"index": 1, "section": "Limitations", "content": "The body names one limitation."},
            ]
        },
    )
    plan_path, corpus_path = _collect_fixture(tmp_path)

    pipeline.collect(plan_path, corpus_path)

    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    source = corpus["sources"][0]
    assert source["evidence_mode"] == "body-text"
    assert source["evidence_digest"]
    assert Path(source["evidence_path"]).read_text(encoding="utf-8").count("### Introduction") == 1
    assert corpus["evidence_coverage"]["body_text_papers"] == 1
    assert corpus["evidence_coverage"]["abstract_only_papers"] == 0


def test_collector_falls_back_to_abstract_only_without_body_text(monkeypatch, tmp_path: Path):
    pipeline = _pipeline()
    _install_libwit(monkeypatch, pipeline, None)
    plan_path, corpus_path = _collect_fixture(tmp_path)

    pipeline.collect(plan_path, corpus_path)

    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    source = corpus["sources"][0]
    assert source["evidence_mode"] == "abstract-only"
    assert source["evidence_path"] == ""
    assert corpus["evidence_coverage"]["body_text_papers"] == 0
    assert corpus["evidence_coverage"]["abstract_only_papers"] == 1


def test_evidence_file_name_stays_inside_the_papers_dir():
    pipeline = _pipeline()

    assert pipeline._evidence_file_name("../../etc/passwd") == "etc-passwd.md"
    assert pipeline._evidence_file_name("..") == "paper.md"
    assert pipeline._evidence_file_name("2607.00001") == "2607.00001.md"


def test_oversized_first_chunk_is_truncated_to_the_per_paper_budget():
    pipeline = _pipeline()
    chunks = [{"index": 0, "section": "Introduction", "content": "word " * 5000}]

    selected = pipeline._ordered_body_chunks(chunks, 100)

    assert len(selected) == 1
    assert selected[0]["tokens"] == 100
    assert len(selected[0]["content"]) <= 400


def test_review_verify_requires_body_text_provenance(tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        "verification_review", ROOT / "recipes" / "verification-technique-review" / "lib" / "review.py"
    )
    assert spec and spec.loader
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)
    source = {
        "source_id": "arxiv:2607.00001",
        "title": "Test paper",
        "evidence_mode": "body-text",
        "evidence_path": str(tmp_path / "papers" / "2607.00001.md"),
    }
    corpus_path = tmp_path / "source-corpus.json"
    corpus_path.write_text(
        json.dumps({"sources": [source], "evidence_coverage": {"body_text_papers": 1}}), encoding="utf-8"
    )
    review._check_evidence({"arxiv:2607.00001": source}, corpus_path)

    corpus_path.write_text(json.dumps({"sources": [source]}), encoding="utf-8")
    try:
        review._check_evidence({"arxiv:2607.00001": source}, corpus_path)
    except review.ReviewError as exc:
        assert "evidence_coverage" in str(exc)
    else:
        raise AssertionError("verify accepted a corpus without evidence coverage")

    source["evidence_path"] = ""
    corpus_path.write_text(
        json.dumps({"sources": [source], "evidence_coverage": {"body_text_papers": 1}}), encoding="utf-8"
    )
    try:
        review._check_evidence({"arxiv:2607.00001": source}, corpus_path)
    except review.ReviewError as exc:
        assert "evidence_path" in str(exc)
    else:
        raise AssertionError("verify accepted a body-text paper with no evidence file")


def test_shard_carries_body_text_metadata(tmp_path: Path):
    pipeline = _pipeline()
    source = _source(1)
    source.update(
        {
            "evidence_mode": "body-text",
            "evidence_path": "/run/papers/2607.00001.md",
            "evidence_chunks": 2,
            "evidence_tokens": 90,
            "evidence_digest": "[Introduction] Body introduction text.",
            "chunk_count": 2,
        }
    )
    corpus_path = tmp_path / "source-corpus.json"
    corpus_path.write_text(json.dumps({"source_count": 1, "required_source_count": 1, "sources": [source]}), encoding="utf-8")
    shard_path = tmp_path / "shard.json"

    pipeline.shard(corpus_path, [shard_path])

    sharded = json.loads(shard_path.read_text(encoding="utf-8"))["sources"][0]
    assert sharded["evidence_mode"] == "body-text"
    assert sharded["evidence_digest"].startswith("[Introduction]")
    assert sharded["evidence_path"] == "/run/papers/2607.00001.md"
