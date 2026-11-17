"""The agent-security-futures deterministic chain, end to end.

The recipe's verifier nodes shell out to ``lib/review.py`` exactly the way these
tests do, so the test exercises the same entry points the run uses. What it
pins is the part a prompt change cannot be trusted with: field names that cross
a node boundary (``severity`` / ``momentum`` / ``readiness`` / ``gap_status``),
the priority formula, the consensus-verdict rule, and the section gate.

The chain is driven with fixtures rather than a model: ten papers, ten themes,
one technique each.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RECIPE = REPO / "recipes" / "agent-security-futures"
LIB = RECIPE / "lib" / "review.py"

SIDS = [f"arxiv:2601.{i:05d}" for i in range(1, 11)]


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _stage(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LIB), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture()
def chain(tmp_path: Path) -> Path:
    """Run catalog -> split -> pack -> merge-reviews, returning the work dir."""
    _write(tmp_path / "source-corpus.json", {
        "sources": [
            {
                "source_id": sid,
                "title": f"Paper {index}",
                "url": f"https://arxiv.org/abs/{sid.split(':')[1]}",
                "published_at": "2026-03-01",
                "evidence_mode": "body-text",
                "evidence_path": f"papers/{sid.split(':')[1]}.md",
            }
            for index, sid in enumerate(SIDS, start=1)
        ],
        "evidence_coverage": {"papers": len(SIDS), "body_text_papers": len(SIDS)},
    })
    _write(tmp_path / "shard-01.json", {
        "shard_id": "01",
        "papers": [
            {
                "source_id": sid,
                "rank": index,
                "title": f"Paper {index}",
                "url": f"https://arxiv.org/abs/{sid.split(':')[1]}",
                "published_at": "2026-03-01",
                "evidence_mode": "body-text",
                "summary": "A mechanism is proposed and measured.",
                "security_relevance": "high",
                "techniques": [{"name": f"Mech {index}", "mechanism": "m", "result": "87% blocked"}],
            }
            for index, sid in enumerate(SIDS, start=1)
        ],
        "shard_techniques": [
            {"technique": f"Technique {index}", "mechanism": "m", "source_ids": [sid]}
            for index, sid in enumerate(SIDS, start=1)
        ],
    })

    result = _stage(
        "catalog",
        "--extraction", tmp_path / "shard-01.json",
        "--corpus", tmp_path / "source-corpus.json",
        "--catalog-out", tmp_path / "technique-catalog.json",
        "--papers-out", tmp_path / "paper-index.json",
    )
    assert result.returncode == 0, result.stderr

    _write(tmp_path / "clusters.json", {
        "groups": [
            {"name": f"Theme {g}", "scope": "s", "technique_keys": [f"T{g:04d}"]}
            for g in range(1, 11)
        ],
    })
    result = _stage(
        "split",
        "--clusters", tmp_path / "clusters.json",
        "--catalog", tmp_path / "technique-catalog.json",
        "--papers", tmp_path / "paper-index.json",
        "--out-dir", tmp_path / "groups",
        "--unclustered-out", tmp_path / "unclustered.json",
    )
    assert result.returncode == 0, result.stderr

    for g in range(1, 11):
        _write(tmp_path / "groups" / f"group-{g:02d}.json", {
            "group_id": f"G{g:02d}",
            "name": f"Theme {g}",
            "scope": "s",
            "techniques": [{
                "id": f"G{g:02d}-T01",
                "technique": f"Theme {g} primary technique",
                "mechanism": "It crosses the boundary.",
                "reported_results": "12 of 15 bypasses (arxiv:2601.00001)",
                "evidence_strength": "moderate",
                "evidence": [SIDS[g - 1]],
                "maturity": "research",
                "trajectory": "Direction: worse before better.",
                "defensive_move": "Mediate the tool call.",
                "test_recipe": "Replay a poisoned issue body.",
            }],
        })

    group_args: list[object] = []
    for g in range(1, 11):
        group_args += ["--group", tmp_path / "groups" / f"group-{g:02d}.json"]
    result = _stage(
        "pack",
        *group_args,
        "--corpus", tmp_path / "source-corpus.json",
        "--state", RECIPE / "context" / "preparedness-state.md",
        "--unclustered", tmp_path / "unclustered.json",
        "--out", tmp_path / "review-pack.json",
    )
    assert result.returncode == 0, result.stderr

    for lane in ("glm", "minimax"):
        _write(tmp_path / f"review-{lane}.json", {
            "reviewer": lane,
            "scores": [
                {
                    "id": f"G{g:02d}-T01",
                    "severity": 4,
                    "momentum": 5,
                    "evidence_confidence": 3,
                    "readiness": "S",
                    "gap_status": "unaddressed",
                    "verdict": "prepare-now" if g <= 3 else "prepare-next",
                    "responsible_owner": "team deploying agents",
                    "rationale": "baseline has no mediation",
                }
                for g in range(1, 11)
            ],
        })

    result = _stage(
        "merge-reviews",
        "--review", tmp_path / "review-glm.json",
        "--review", tmp_path / "review-minimax.json",
        "--pack", tmp_path / "review-pack.json",
        "--out", tmp_path / "merged-review.json",
    )
    assert result.returncode == 0, result.stderr
    return tmp_path


REPORT_BODY = [
    "# Agent Security 2026",
    "## Executive Summary", "Worst first (G01-T01).",
    "## Trending Now", "Theme G02-T01 moved.",
    "## Forecast", "Direction (G03-T01).",
    "## Human Impact", "People bear it.",
    "## How to Prepare", "Mediate (G04-T01).",
    "",
]


def _assemble(chain: Path, body: list[str]) -> subprocess.CompletedProcess[str]:
    (chain / "report.md").write_text("\n".join(body), encoding="utf-8")
    return _stage(
        "assemble",
        "--report", chain / "report.md",
        "--merged", chain / "merged-review.json",
        "--corpus", chain / "source-corpus.json",
        "--out", chain / "agent-security-futures.md",
    )


def test_chain_ranks_by_severity_momentum_confidence_over_readiness(chain: Path) -> None:
    merged = json.loads((chain / "merged-review.json").read_text(encoding="utf-8"))
    top = merged["ranked"][0]
    # 4 * 5 * 3 / 1.0, unaddressed, so no discount.
    assert top["priority"] == 60.0
    assert top["mean_severity"] == 4.0
    assert top["mean_momentum"] == 5.0
    assert top["verdict"] == "prepare-now"
    assert top["maturity"] == "research"
    assert top["trajectory"].startswith("Direction")
    assert top["defensive_move"] == "Mediate the tool call."


def test_commoditised_gap_zeroes_priority_while_the_report_still_verifies(chain: Path) -> None:
    for lane in ("glm", "minimax"):
        path = chain / f"review-{lane}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["scores"][0]["gap_status"] = "commoditised"
        path.write_text(json.dumps(payload), encoding="utf-8")
    result = _stage(
        "merge-reviews",
        "--review", chain / "review-glm.json",
        "--review", chain / "review-minimax.json",
        "--pack", chain / "review-pack.json",
        "--out", chain / "merged-review.json",
    )
    assert result.returncode == 0, result.stderr
    merged = json.loads((chain / "merged-review.json").read_text(encoding="utf-8"))
    zeroed = [row for row in merged["ranked"] if row["id"] == "G01-T01"][0]
    assert zeroed["priority"] == 0.0

    assert _assemble(chain, REPORT_BODY + ["## What Would Change This Forecast", "- G05-T01.", ""]).returncode == 0
    result = _stage(
        "verify",
        "--final", chain / "agent-security-futures.md",
        "--merged", chain / "merged-review.json",
        "--corpus", chain / "source-corpus.json",
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["groups"] == 10
    assert summary["body_text_papers"] == len(SIDS)


def test_verify_rejects_a_report_without_the_forecast_invalidation_section(chain: Path) -> None:
    assert _assemble(chain, REPORT_BODY).returncode == 0
    result = _stage(
        "verify",
        "--final", chain / "agent-security-futures.md",
        "--merged", chain / "merged-review.json",
        "--corpus", chain / "source-corpus.json",
    )
    assert result.returncode != 0
    assert "What Would Change This Forecast" in result.stderr
