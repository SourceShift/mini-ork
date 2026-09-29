"""Regression tests for the rsi-technique-review recipe's deterministic stages."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from mini_ork.workflow.compiler import compile_workflow


ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "recipes" / "rsi-technique-review"


def _lib():
    spec = importlib.util.spec_from_file_location("rsi_review", RECIPE / "lib" / "rsi_review.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _corpus(tmp_path: Path, n: int = 40) -> Path:
    sources = [
        {
            "source_id": f"arxiv:2607.{i:05d}",
            "rank": i,
            "title": f"RSI paper {i}",
            "url": f"https://arxiv.org/abs/2607.{i:05d}",
            "published_at": "2026-07-01",
        }
        for i in range(1, n + 1)
    ]
    return _write(tmp_path / "corpus.json", {"sources": sources, "required_source_count": n})


def _extractions(tmp_path: Path, n: int = 40) -> list[Path]:
    paths = []
    for shard in range(10):
        ids = [f"arxiv:2607.{i:05d}" for i in range(shard * (n // 10) + 1, (shard + 1) * (n // 10) + 1)]
        paths.append(
            _write(
                tmp_path / "ex" / f"shard-{shard + 1:02d}.json",
                {
                    "shard_id": f"{shard + 1:02d}",
                    "papers": [{"source_id": sid, "summary": "s", "techniques": [{"name": "t", "mechanism": "m"}]} for sid in ids],
                    "shard_techniques": [
                        {"technique": f"tech {shard}-{k}", "mechanism": "m", "source_ids": ids[k::2] + ["arxiv:2699.00001"]}
                        for k in range(2)
                    ],
                },
            )
        )
    return paths


def test_workflow_pins_every_llm_node_to_glm_or_minimax():
    compiled = compile_workflow(RECIPE / "workflow.yaml")
    raw = yaml.safe_load((RECIPE / "workflow.yaml").read_text())
    llm = [n for n in raw["nodes"] if n["type"] == "researcher"]
    assert {n["model_lane"] for n in llm} == {"glm_lens", "minimax_lens"}
    assert len([n for n in compiled.nodes if n.startswith("extract_shard_")]) == 10
    assert len(compiled.bindings_for("technique_catalog")) == 11  # 10 shards + corpus
    assert len(compiled.bindings_for("review_merger")) == 3  # 2 reviews + pack


def test_pipeline_catalog_split_pack_merge_assemble_verify(tmp_path: Path):
    lib = _lib()
    corpus = _corpus(tmp_path)
    lib.catalog(_extractions(tmp_path), corpus, tmp_path / "cat.json", tmp_path / "papers.json")
    cat = json.loads((tmp_path / "cat.json").read_text())
    assert cat["coverage"]["covered"] == 40
    assert cat["dropped_unknown_source_ids"] == ["arxiv:2699.00001"]
    keys = [t["key"] for t in cat["techniques"]]
    _write(tmp_path / "clusters.json", {"groups": [{"name": f"g{i}", "technique_keys": keys[i::10]} for i in range(10)]})
    lib.split(tmp_path / "clusters.json", tmp_path / "cat.json", tmp_path / "papers.json", tmp_path / "groups", tmp_path / "unc.json")
    for group_file in sorted((tmp_path / "groups").glob("*.json")):
        group = json.loads(group_file.read_text())
        gid = group["group_id"]
        _write(
            tmp_path / "gx" / group_file.name,
            {"group_id": gid, "techniques": [{"id": f"{gid}-T01", "technique": "x", "evidence": group["techniques"][0]["source_ids"]}]},
        )
    state = tmp_path / "state.md"
    state.write_text("state")
    lib.pack(sorted((tmp_path / "gx").glob("*.json")), corpus, state, tmp_path / "unc.json", tmp_path / "pack.json")
    ids = [t["id"] for t in json.loads((tmp_path / "pack.json").read_text())["techniques"]]
    for who, impact in (("glm", 5), ("minimax", 2)):
        _write(
            tmp_path / f"rev-{who}.json",
            {"reviewer": who, "scores": [{"id": i, "impact": impact, "evidence_confidence": 4, "effort": "S", "verdict": "implement-now" if who == "glm" else "watch"} for i in ids]},
        )
    lib.merge_reviews([tmp_path / "rev-glm.json", tmp_path / "rev-minimax.json"], tmp_path / "pack.json", tmp_path / "merged.json")
    merged = json.loads((tmp_path / "merged.json").read_text())
    top = merged["ranked"][0]
    assert top["verdict"] == "watch"  # conservative consensus
    assert top["disagreement"] is True
    report = tmp_path / "report.md"
    report.write_text(f"# R\n\n## Executive Summary\n\nx\n\n## Implement Now\n\n{top['id']} ({top['evidence'][0]})\n")
    lib.assemble(report, tmp_path / "merged.json", corpus, tmp_path / "final.md")
    lib.verify(tmp_path / "final.md", tmp_path / "merged.json", corpus)

    report.write_text(report.read_text() + "\nalso arxiv:2699.00001\n")
    lib.assemble(report, tmp_path / "merged.json", corpus, tmp_path / "bad.md")
    with pytest.raises(lib.ReviewError, match="absent from the corpus"):
        lib.verify(tmp_path / "bad.md", tmp_path / "merged.json", corpus)


def test_catalog_fails_below_coverage_floor(tmp_path: Path):
    lib = _lib()
    corpus = _corpus(tmp_path, n=80)  # extractions cover only 40
    with pytest.raises(lib.ReviewError, match="coverage floor"):
        lib.catalog(_extractions(tmp_path), corpus, tmp_path / "c.json", tmp_path / "p.json")


def test_priority_zeroes_already_shipped_and_halves_extends():
    lib = _lib()
    base = {"impact": 4, "evidence_confidence": 3, "effort": "S", "verdict": "next"}
    assert lib.priority([{**base, "novelty_vs_shipped": "new"}]) == 12.0
    assert lib.priority([{**base, "novelty_vs_shipped": "extends"}]) == 6.0
    assert lib.priority([{**base, "novelty_vs_shipped": "already-shipped"}] * 2) == 0.0


def test_verify_accepts_versioned_corpus_ids(tmp_path: Path):
    lib = _lib()
    corpus = _write(tmp_path / "c.json", {"sources": [{"source_id": "arxiv:2604.05112v1", "url": "u"}]})
    merged = _write(tmp_path / "m.json", {"ranked": [{"id": "G01-T01"}]})
    groups = "".join(f"### G{i:02d} — g\n" for i in range(1, 11))
    final = tmp_path / "f.md"
    final.write_text("## Executive Summary\nG01-T01 (arxiv:2604.05112v1, arxiv:2604.05112)\n## Implement Now\n"
                     "## Appendix A\n## Appendix B\n" + groups)
    lib.verify(final, merged, corpus)
