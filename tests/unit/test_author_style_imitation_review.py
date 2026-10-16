"""Tests for the author-style-imitation-review recipe.

Two halves, and the split is deliberate:

* **Structural** — the DAG and the artifact contract must keep declaring the
  *deterministic* assignment. The model defines the eight families; the
  pipeline sorts the techniques. If the workflow ever goes back to asking the
  clusterer for a technique→family mapping, that is the regression this file
  exists to catch (a model cannot reliably emit a 396-entry dict, and the
  attempt cost ~35 minutes of self-debugging on the 2026-10-16 run).
* **Behavioural** — ``assign()`` is exercised both in-process (placements,
  empty-family repair, audit shape) and through its CLI (exit codes), because
  the verifier talks to the CLI, not the function.

The end-to-end test drives the whole deterministic chain — 1000-paper corpus,
catalogue, assignment, split, group extraction, two reviews, merge, report,
verify — with no network and no LLM, and ends on the three negative controls.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "recipes" / "author-style-imitation-review"
LIB = RECIPE / "lib" / "style_pipeline.py"

# The corpus floor and stage counts the collection plan and workflow.yaml agree
# on. Bumping one without the others is a real failure mode, so they are pinned
# here rather than read from the environment.
CORPUS_FLOOR = 1000
SHARDS = 10
GROUPS = 8
BATCHES = 5


def _pipeline():
    spec = importlib.util.spec_from_file_location("author_style_pipeline", LIB)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_env(run_dir: Path) -> dict[str, str]:
    """The env every deterministic stage expects.

    MO_STYLE_* must match the nodes in workflow.yaml; MINI_ORK_RESEARCH_MIN_SOURCES
    is the corpus floor the merger refuses to go below.
    """
    return dict(
        os.environ,
        MINI_ORK_RUN_DIR=str(run_dir),
        MO_STYLE_SEARCH_BATCHES=str(BATCHES),
        MO_STYLE_SHARD_COUNT=str(SHARDS),
        MO_STYLE_GROUP_COUNT=str(GROUPS),
        MINI_ORK_RESEARCH_MIN_SOURCES=str(CORPUS_FLOOR),
    )


def _cli(run_dir: Path, *args: object, expect_rc: int = 0) -> subprocess.CompletedProcess:
    """Invoke the pipeline CLI the way the verifiers do — a real subprocess.

    sys.executable, not a hard-coded interpreter: the gate runs under whatever
    python the repo targets, and pinning one here would make the test lie about
    the environment the verifiers actually get.
    """
    proc = subprocess.run(
        [sys.executable, str(LIB), *map(str, args)],
        env=_run_env(run_dir),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == expect_rc, (
        f"expected rc={expect_rc}, got {proc.returncode}\n"
        f"stdout: {proc.stdout[-800:]}\nstderr: {proc.stderr[-800:]}"
    )
    return proc


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


# --------------------------------------------------------------------------
# Structural: the contract stays deterministic
# --------------------------------------------------------------------------


def test_workflow_declares_families_json_not_a_mapping():
    """technique_clusterer's only output is the family definitions.

    The node must publish families.json. clusters.json is produced downstream by
    the deterministic assign stage, so a workflow that still routes
    technique_clusters to a clusters.json path has regressed to the model-written
    mapping.
    """
    raw = yaml.safe_load((RECIPE / "workflow.yaml").read_text())
    clusterers = [n for n in raw["nodes"] if n["name"] == "technique_clusterer"]
    assert clusterers, "technique_clusterer node is missing from workflow.yaml"
    outputs = clusterers[0]["outputs"]
    assert [o["name"] for o in outputs] == ["technique_clusters"]
    assert outputs[0]["path"] == "families.json", (
        "technique_clusterer must publish families.json; clusters.json is the "
        "deterministic assign stage's output"
    )


def test_artifact_contract_declares_three_cluster_artifacts():
    contract = yaml.safe_load((RECIPE / "artifact_contract.yaml").read_text())
    declared = {Path(o["path"]).name for o in contract["outputs"]}
    for name in ("families.json", "clusters.json", "cluster-assignment.json"):
        assert name in declared, f"artifact_contract.yaml no longer declares {name}"
    # The audit file is the point of the design: an assignment nobody can
    # inspect is no better than the model mapping it replaced.
    assert "cluster-assignment" in " ".join(declared)


def test_cluster_splitter_runs_assign_before_split():
    """The verifier drives assign then split, in that order.

    Order matters — split consumes the clusters.json that assign writes. A
    verifier that ran them the other way would read a stale or absent file.
    """
    source = (RECIPE / "verifiers" / "split.py").read_text()
    assert '"assign"' in source and '"split"' in source
    assert source.index('"assign"') < source.index('"split"'), "assign must run before split"

    raw = yaml.safe_load((RECIPE / "workflow.yaml").read_text())
    splitter = [n for n in raw["nodes"] if n["name"] == "cluster_splitter"]
    assert splitter, "cluster_splitter node is missing"
    # inputs is a mapping of name -> {"required": bool}; only the names matter here.
    assert set(splitter[0]["inputs"]) == {"technique_clusters", "technique_catalog", "paper_index"}


def test_clusterer_prompt_asks_for_families_only():
    """The prompt must not ask the model to sort the catalogue itself."""
    prompt = (RECIPE / "prompts" / "cluster-techniques.md").read_text().lower()
    assert "families" in prompt
    # The exact failure that motivated the split: a hand-written key→family map.
    assert "technique_keys" not in prompt
    assert "do not emit a mapping" in prompt


# --------------------------------------------------------------------------
# Behavioural: assign() itself
# --------------------------------------------------------------------------


def _catalog(techniques: list[tuple[str, str]]) -> dict:
    return {
        "techniques": [
            {
                "key": f"T{i:04d}",
                "technique": name,
                "mechanism": mech,
                "source_ids": ["arxiv:2509.14543"],
            }
            for i, (name, mech) in enumerate(techniques, start=1)
        ]
    }


def _eight_families() -> list[dict]:
    """Eight families, four of which match real vocabulary in the fixture.

    The other four carry nonsense keywords on purpose: they must be filled by
    the repair loop, because a family with no members is a hard failure in
    split().
    """
    return [
        {"name": "Style evidence & retrieval", "scope": "which samples enter context",
         "criteria": "retrieval selection", "keywords": ["retrieval", "reranker", "similarity"],
         "exemplars": []},
        {"name": "Parameter-level adaptation", "scope": "the model itself",
         "criteria": "adapters and weights", "keywords": ["lora", "adapter", "fine-tune"],
         "exemplars": []},
        {"name": "Post-hoc revision & critique", "scope": "draft then rewrite",
         "criteria": "critic rewrites", "keywords": ["critic", "rewrite", "revision"],
         "exemplars": []},
        {"name": "Measurement & evaluation", "scope": "metrics that decide",
         "criteria": "attribution and verification", "keywords": ["verification", "impostor", "threshold"],
         "exemplars": []},
        *[
            {"name": f"Filler family {g}", "scope": f"unmatched family {g}",
             "criteria": "no shared vocabulary", "keywords": [f"zzq{g}", f"vvk{g}"], "exemplars": []}
            for g in range(5, 9)
        ],
    ]


def test_assign_places_techniques_and_repairs_empty_families(tmp_path: Path):
    """Every technique lands somewhere, and no family is left empty.

    Unmatched vocabulary is the normal case for real catalogues — the model's
    keywords are never guaranteed to appear verbatim in a technique's prose — so
    the repair loop, not the scoring, is what keeps this usable.
    """
    _write_json(tmp_path / "catalog.json", _catalog([
        ("Dense retrieval of the author's prior posts", "retrieve samples by embedding similarity reranker"),
        ("Low-rank adapter fine-tuning per author", "LoRA adapter fine-tune weights"),
        ("Critique-and-revise style editing loop", "critic rewrites revision of the draft"),
        ("Authorship verification with contrastive loss", "verification same-author impostor threshold"),
        *[(f"Unlabelled technique {i}", "zxqv wlkm nnnn") for i in range(5, 11)],
    ]))
    families = _eight_families()
    _write_json(tmp_path / "families.json", {"families": families})

    _pipeline().assign(
        tmp_path / "families.json",
        tmp_path / "catalog.json",
        tmp_path / "clusters.json",
        tmp_path / "cluster-assignment.json",
    )

    groups = json.loads((tmp_path / "clusters.json").read_text())["groups"]
    assert len(groups) == GROUPS
    assert all(g["technique_keys"] for g in groups), "assign left a family empty"

    placed = {key: g["name"] for g in groups for key in g["technique_keys"]}
    assert placed["T0001"] == "Style evidence & retrieval"
    assert placed["T0002"] == "Parameter-level adaptation"
    assert placed["T0003"] == "Post-hoc revision & critique"
    assert placed["T0004"] == "Measurement & evaluation"

    audit = json.loads((tmp_path / "cluster-assignment.json").read_text())
    assert audit["family_count"] == GROUPS
    assert audit["technique_count"] == len(placed)
    assert sum(audit["family_sizes"]) == len(placed), "audit sizes must account for every technique"
    assert audit["repaired"], "unmatched families should have been repaired, not left empty"


def test_assign_rejects_a_wrong_family_count(tmp_path: Path):
    """A clusterer that defines seven families must fail, not silently proceed.

    split() derives group ids positionally from the list, so a short list would
    shift every downstream group rather than error.
    """
    _write_json(tmp_path / "catalog.json", _catalog([
        (f"Technique {i}", "some mechanism text here") for i in range(1, 11)
    ]))
    _write_json(tmp_path / "families.json", {"families": _eight_families()[:7]})

    _cli(
        tmp_path,
        "assign",
        "--families", tmp_path / "families.json",
        "--catalog", tmp_path / "catalog.json",
        "--clusters-out", tmp_path / "clusters.json",
        "--audit-out", tmp_path / "audit.json",
        expect_rc=1,
    )


def test_assign_is_deterministic(tmp_path: Path):
    """Same inputs, same assignment — the property the model could not offer."""
    _write_json(tmp_path / "catalog.json", _catalog([
        ("Dense retrieval of prior posts", "embedding similarity reranker"),
        ("Adapter fine-tuning", "lora weights"),
        ("Critique revise loop", "critic revision"),
        *[(f"Unlabelled {i}", "zxqv wlkm") for i in range(4, 12)],
    ]))
    _write_json(tmp_path / "families.json", {"families": _eight_families()})

    def once(tag: str) -> str:
        out = tmp_path / f"clusters-{tag}.json"
        _pipeline().assign(
            tmp_path / "families.json", tmp_path / "catalog.json", out, tmp_path / f"a-{tag}.json"
        )
        return out.read_text()

    assert once("1") == once("2")


# --------------------------------------------------------------------------
# End-to-end: the whole deterministic chain, no LLM, no network
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def chain(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run every deterministic stage once; individual tests assert on the output."""
    run = tmp_path_factory.mktemp("style-run")
    for sub in ("queries", "corpus", "shards", "extractions", "groups", "group-techniques", "reviews"):
        (run / sub).mkdir(parents=True, exist_ok=True)

    # 1. plan-batches: the collection plan must split into 5 batches of 10.
    _cli(run, "plan-batches", "--plan", RECIPE / "collection-plan.json", "--out-dir", run / "queries")
    batches = sorted((run / "queries").glob("batch-*.json"))
    assert len(batches) == BATCHES
    assert all(len(json.loads(b.read_text())["queries"]) == 10 for b in batches)

    # 2. a synthetic corpus at the floor, anchor first. Written directly rather
    #    than through `merge`: merge is a union + re-rank + floor check, and both
    #    halves of that are pinned by their own test below.
    sources = []
    for i in range(CORPUS_FLOOR):
        sid = "arxiv:2509.14543" if i == 0 else f"arxiv:24{(i % 12):02d}.{10000 + i:05d}"
        sources.append({
            "source_id": sid,
            "title": f"Paper {i}",
            "url": f"https://arxiv.org/abs/{sid.split(':')[1]}",
            "published_at": "2024-05-01",
            "rank": i + 1,
            "abstract": "x" * 800,
            "topics": ["style-imitation"],
            "citation_count": i % 7,
            "influential_citation_count": 0,
            "relevance_score": 0.5,
            "retrieved_at": "2026-10-16T00:00:00Z",
            "ranking_score": CORPUS_FLOOR - i,
        })
    assert len({s["source_id"] for s in sources}) == CORPUS_FLOOR
    _write_json(run / "source-corpus.json", {
        "source_count": CORPUS_FLOOR,
        "required_source_count": CORPUS_FLOOR,
        "sources": sources,
        "retrieval_at": "",
        "search_notes": [],
    })

    # 3. shard into 10 x 100.
    _cli(
        run,
        "shard",
        *sum([["--output", run / "shards" / f"source-shard-{i:02d}.json"] for i in range(1, SHARDS + 1)], []),
        "--input", run / "source-corpus.json",
    )
    assert len(list((run / "shards").glob("*.json"))) == SHARDS

    # 4. per-shard extraction, then the catalogue.
    for i in range(1, SHARDS + 1):
        shard = json.loads((run / "shards" / f"source-shard-{i:02d}.json").read_text())
        papers = [
            {
                "source_id": s["source_id"], "title": s["title"],
                "published_at": s["published_at"], "rank": s["rank"],
                "summary": f"Summary of {s['title']}: method, result.",
                "relevance": "high",
                "techniques": [{"name": f"Tech {i}-a", "mechanism": "does a thing", "result": "12% gain"}],
            }
            for s in shard["sources"]
        ]
        _write_json(run / "extractions" / f"shard-{i:02d}.json", {
            "shard_id": f"{i:02d}",
            "papers": papers,
            "shard_techniques": [
                {"technique": f"Canonical tech {i}", "mechanism": "canonical merges variants across papers",
                 "source_ids": [p["source_id"] for p in papers[:5]]},
                {"technique": f"Solo tech {i}", "mechanism": "single paper solo technique",
                 "source_ids": [papers[-1]["source_id"]]},
            ],
        })
    _cli(
        run,
        "catalog",
        "--corpus", run / "source-corpus.json",
        "--catalog-out", run / "technique-catalog.json",
        "--papers-out", run / "paper-index.json",
        *sum([["--extraction", run / "extractions" / f"shard-{i:02d}.json"] for i in range(1, SHARDS + 1)], []),
    )
    catalogue = json.loads((run / "technique-catalog.json").read_text())
    assert catalogue["coverage"]["covered"] == CORPUS_FLOOR

    # 5. the model defines the families; assign sorts the catalogue into them.
    _write_json(run / "families.json", {"families": _eight_families()})
    _cli(
        run,
        "assign",
        "--families", run / "families.json",
        "--catalog", run / "technique-catalog.json",
        "--clusters-out", run / "clusters.json",
        "--audit-out", run / "cluster-assignment.json",
    )
    clusters = json.loads((run / "clusters.json").read_text())
    assert len(clusters["groups"]) == GROUPS
    assert all(g["technique_keys"] for g in clusters["groups"])

    _cli(
        run,
        "split",
        "--clusters", run / "clusters.json",
        "--catalog", run / "technique-catalog.json",
        "--papers", run / "paper-index.json",
        "--out-dir", run / "groups",
        "--unclustered-out", run / "unclustered.json",
    )
    assert len(list((run / "groups").glob("group-*.json"))) == GROUPS

    # 6. per-group implementable techniques with evidence drawn from the group.
    for i in range(1, GROUPS + 1):
        group = json.loads((run / "groups" / f"group-{i:02d}.json").read_text())
        pool = sorted({p["source_id"] for p in group["papers"]})
        techniques = []
        for n in range(5):
            evidence = pool[n * 2:n * 2 + 3] or [pool[0]]
            techniques.append({
                "id": f"G{i:02d}-T{n + 1:02d}",
                "technique": f"Implementable {i}-{n}",
                "mechanism": "m" * 120,
                "reported_results": f"10 pct gain (arxiv:{evidence[0].split(':')[1]})",
                "evidence_strength": "strong",
                "evidence": evidence,
                "transferability": "needs 20+ samples",
                "adoption_sketch": "run it, measure",
            })
        _write_json(run / "group-techniques" / f"group-{i:02d}.json", {
            "group_id": f"G{i:02d}", "name": group["name"],
            "scope": group["scope"], "techniques": techniques,
        })

    _cli(
        run,
        "pack",
        "--corpus", run / "source-corpus.json",
        "--state", RECIPE / "context" / "style-imitation-state.md",
        "--unclustered", run / "unclustered.json",
        "--out", run / "review-pack.json",
        *sum([["--group", run / "group-techniques" / f"group-{i:02d}.json"] for i in range(1, GROUPS + 1)], []),
    )

    # 7. two independent reviewers, then the merge.
    pack = json.loads((run / "review-pack.json").read_text())
    ids = [t["id"] for t in pack["techniques"]]
    assert ids, "pack produced no techniques"
    for lane, bias in (("glm", 0), ("minimax", -1)):
        _write_json(run / "reviews" / f"review-{lane}.json", {
            "reviewer": lane,
            "scores": [
                {
                    "id": tid, "impact": 4 + bias, "evidence_confidence": 4, "effort": "M",
                    "evidence_verdict": "supported" if n % 5 else "overclaimed",
                    "verdict": "adopt-now" if n < 12 else ("next" if n < 25 else "watch"),
                    "landing_surface": "prompt assembly", "rationale": "because",
                }
                for n, tid in enumerate(ids)
            ],
        })
    _cli(
        run,
        "merge-reviews",
        "--pack", run / "review-pack.json",
        "--out", run / "merged-review.json",
        "--review", run / "reviews" / "review-glm.json",
        "--review", run / "reviews" / "review-minimax.json",
    )

    # 8. report, assemble, verify.
    merged = json.loads((run / "merged-review.json").read_text())
    body = [
        "# Imitating an Author's Writing Style — What the Literature Says Works", "",
        "## Executive Summary", "",
        "In-context conditioning works in news and email and struggles in blogs and forums "
        "(arxiv:2509.14543).", "",
        "## Technique Groups", "",
        "```mermaid", "flowchart LR", "  A[evidence] --> B[conditioning]", "```", "",
    ]
    for group in clusters["groups"]:
        body += [f"Family {group['name']}: " + ", ".join(group["technique_keys"][:3]), ""]
    body += ["## Techniques That Work", ""]
    body += [f"- {r['id']} {r['technique']} — {len(r['evidence'])} papers" for r in merged["ranked"][:12]]
    body += ["", "## Skipped", "", "nothing", ""]
    (run / "technique-report.md").write_text("\n".join(body))

    _cli(run, "assemble",
         "--report", run / "technique-report.md",
         "--merged", run / "merged-review.json",
         "--corpus", run / "source-corpus.json",
         "--out", run / "author-style-imitation-review.md")
    _cli(run, "verify",
         "--final", run / "author-style-imitation-review.md",
         "--merged", run / "merged-review.json",
         "--corpus", run / "source-corpus.json")

    # Everything the three declared cluster artifacts are supposed to be.
    assert (run / "families.json").exists()
    assert (run / "clusters.json").exists()
    assert (run / "cluster-assignment.json").exists()
    return run


def test_chain_produces_all_three_declared_cluster_artifacts(chain: Path):
    """The acceptance condition for the deterministic assignment.

    The artifact contract declares families.json, clusters.json and
    cluster-assignment.json. The first successful live run (2026-10-16) produced
    only clusters.json, in the old model-authored shape — this asserts the chain
    now produces all three, in the declared shape, with nothing unassigned.
    """
    families = json.loads((chain / "families.json").read_text())
    assert len(families["families"]) == GROUPS

    clusters = json.loads((chain / "clusters.json").read_text())
    assert len(clusters["groups"]) == GROUPS

    audit = json.loads((chain / "cluster-assignment.json").read_text())
    assert audit["family_count"] == GROUPS
    assert sum(audit["family_sizes"]) == audit["technique_count"]

    unclustered = json.loads((chain / "unclustered.json").read_text())
    assert not (unclustered.get("unclustered") or unclustered.get("techniques")), (
        f"every technique must be assigned, got {unclustered}"
    )


def test_chain_verify_reports_full_coverage(chain: Path):
    """The verify stage's summary, read from stdout.

    Not from verifier_verify.json — that file is written by the engine's
    verifier harness, not by this CLI, so a test that read it would be asserting
    on the harness rather than on the pipeline.
    """
    proc = _cli(
        chain,
        "verify",
        "--final", chain / "author-style-imitation-review.md",
        "--merged", chain / "merged-review.json",
        "--corpus", chain / "source-corpus.json",
    )
    summaries = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            payload = json.loads(line)
            if isinstance(payload, dict) and "groups" in payload:
                summaries.append(payload)
    assert summaries, f"verify printed no summary JSON: {proc.stdout[-500:]}"
    out = summaries[0]
    assert out["groups"] == GROUPS
    assert out["corpus_papers"] == CORPUS_FLOOR
    assert out["techniques"] > 0


def test_split_rejects_a_seven_group_cluster_file(chain: Path):
    """split() derives group ids positionally, so a short list must fail loudly."""
    short = chain / "clusters7.json"
    _write_json(short, {"groups": json.loads((chain / "clusters.json").read_text())["groups"][:7]})
    _cli(
        chain,
        "split",
        "--clusters", short,
        "--catalog", chain / "technique-catalog.json",
        "--papers", chain / "paper-index.json",
        "--out-dir", chain / "groups7",
        "--unclustered-out", chain / "u7.json",
        expect_rc=1,
    )


def test_verify_rejects_an_off_corpus_citation(chain: Path):
    """The whole point of the contract: a report may not cite outside the corpus."""
    final = chain / "author-style-imitation-review.md"
    bad = chain / "bad.md"
    bad.write_text(final.read_text().replace("arxiv:2509.14543", "arxiv:9999.99999", 1))
    _cli(
        chain,
        "verify",
        "--final", bad,
        "--merged", chain / "merged-review.json",
        "--corpus", chain / "source-corpus.json",
        expect_rc=1,
    )


def test_merge_refuses_a_corpus_below_the_floor(chain: Path, tmp_path: Path):
    """A searcher shortfall must fail, not quietly shrink every later stage.

    The plan asks for 1000; the recipe floor is the same number; here the
    searchers return 900. The merger must refuse rather than hand 900 papers to
    the sharders and let the loss surface as a weaker review.
    """
    short = [
        {
            "source_id": f"arxiv:2401.{i:05d}",
            "title": f"Paper {i}",
            "url": f"https://arxiv.org/abs/2401.{i:05d}",
            "published_at": "2024-05-01",
            "rank": i + 1,
            "topics": ["style-imitation"],
            "citation_count": 0,
            "influential_citation_count": 0,
            "relevance_score": 0.5,
        }
        for i in range(CORPUS_FLOOR - 100)
    ]
    partial = tmp_path / "partial-01.json"
    _write_json(partial, {"batch_id": "01", "sources": short})

    _cli(
        chain,
        "merge",
        "--plan", RECIPE / "collection-plan.json",
        "--out", tmp_path / "merged.json",
        "--partial", partial,
        expect_rc=1,
    )
