"""Structural tests for the repo-practices-benchmark recipe.

The recipe exists to answer one question ("what does this repo do that we do
not?") without producing confident prose nobody can check. These tests pin the
two things that make that true: the lenses run on distinct model families
(two models from one family agreeing is not evidence), and the citation gate
fails when a report cites a path that does not exist.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from mini_ork.workflow.compiler import compile_workflow


ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "recipes" / "repo-practices-benchmark"
VERIFIER = RECIPE / "verifiers" / "evidence-citations.py"
LENSES = ("conventions", "pipeline", "architecture", "agent-ux")


def test_workflow_compiles_with_four_lens_families():
    compiled = compile_workflow(RECIPE / "workflow.yaml")
    names = list(compiled.nodes)
    assert names == [
        "planner",
        "conventions_lens",
        "pipeline_lens",
        "architecture_lens",
        "agent_ux_lens",
        "synthesizer",
        "evidence_density",
        "publisher",
        "rollback",
    ]
    raw = yaml.safe_load((RECIPE / "workflow.yaml").read_text())
    lanes = {
        n["name"]: n["model_lane"]
        for n in raw["nodes"]
        if n["name"].endswith("_lens")
    }
    assert len(lanes) == 4
    # Four distinct lane aliases; whether two aliases resolve to the same
    # provider at dispatch time is an operator concern, not the recipe's.
    assert len(set(lanes.values())) == 4


def test_every_verifier_ref_exists():
    raw = yaml.safe_load((RECIPE / "workflow.yaml").read_text())
    for node in raw["nodes"]:
        if node["type"] == "verifier":
            assert (RECIPE / node["verifier_ref"]).is_file(), node["verifier_ref"]


def _seed_roots(reference: Path, subject: Path) -> None:
    """Eight distinct files, four per root — the gate counts distinct paths."""
    for i in range(4):
        (reference / f"ref{i}.py").write_text("x = 1\n")
        (subject / f"sub{i}.py").write_text("y = 2\n")


def _write_fixture_run(run_dir: Path, reference: Path, subject: Path) -> None:
    """Four lens reports that each cite real files, plus a 6-item synthesis."""
    for stem in LENSES:
        body = "\n".join(
            [f"ref claim {i} — `{reference / f'ref{i}.py'}:1`" for i in range(4)]
            + [f"sub claim {i} — `{subject / f'sub{i}.py'}:1`" for i in range(4)])
        (run_dir / f"lens-{stem}.md").write_text(
            f"# {stem}\n\n{body}\n" + "filler line\n" * 20)
    items = "\n".join(f"### {i}. item {i}\n" for i in range(1, 7))
    (run_dir / "synthesis.md").write_text(
        f"# Synthesis\n\n{items}\n"
        + "".join(f"see `lens-{stem}.md`\n" for stem in LENSES)
        + "\nWhat mini-ork already does well: plenty.\n")


def _run_verifier(run_dir: Path, reference: Path, subject: Path) -> dict:
    env = {
        **os.environ,
        "MINI_ORK_RUN_DIR": str(run_dir),
        "BENCH_REFERENCE_ROOT": str(reference),
        "BENCH_SUBJECT_ROOT": str(subject),
    }
    proc = subprocess.run(
        [sys.executable, str(VERIFIER)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_citation_gate_passes_a_grounded_run(tmp_path):
    reference, subject = tmp_path / "ref", tmp_path / "sub"
    run_dir = tmp_path / "run"
    for d in (reference, subject, run_dir):
        d.mkdir()
    _seed_roots(reference, subject)
    _write_fixture_run(run_dir, reference, subject)

    result = _run_verifier(run_dir, reference, subject)
    assert result["pass"] is True, result["missing"]
    assert result["citations_resolved"] >= len(LENSES) * 8
    assert Path(result["evidence_path"]).is_file()


def test_citation_gate_fails_a_lens_that_cites_nothing_real(tmp_path):
    """The failure mode this gate exists for: confident prose, invented paths.

    An *individual* unresolved token is not fatal — a glob (`.agents/skills/*`),
    a directory, or a bare filename legitimately reads that way, and the real
    Warp run logged 80 of them in a sound synthesis. What is fatal is a report
    whose citations mostly do not resolve, which is what the floor measures.
    """
    reference, subject = tmp_path / "ref", tmp_path / "sub"
    run_dir = tmp_path / "run"
    for d in (reference, subject, run_dir):
        d.mkdir()
    _seed_roots(reference, subject)
    _write_fixture_run(run_dir, reference, subject)

    invented = [reference / f"no-such-file-{i}.py" for i in range(8)]
    (run_dir / "lens-pipeline.md").write_text(
        "# pipeline\n\n"
        + "\n".join(f"claim — `{p}:{i}`" for i, p in enumerate(invented))
        + "\n" + "filler line\n" * 20)

    result = _run_verifier(run_dir, reference, subject)
    assert result["pass"] is False
    assert any("lens-pipeline.md" in m for m in result["missing"]), result["missing"]
    log = Path(result["evidence_path"]).read_text()
    assert "no-such-file-0.py" in log
