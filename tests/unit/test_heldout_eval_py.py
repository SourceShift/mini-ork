"""Unit tests for scripts/heldout_eval.py — the pure JSON-driven scorer.

Covers: resolve_per_dollar math on a known fixture, --budget-usd caps
tasks_scored deterministically in manifest order, zero-cost yields
resolve_per_dollar=0.0, and arm/epoch passthrough.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "heldout_eval.py"
MANIFEST = REPO / "evals" / "heldout" / "manifest.json"


def _run_cli(results: dict, *, arm: str = "off", epoch: int = 0, budget_usd: float | None = None,
             out_path: Path | None = None) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(results, f)
        results_path = Path(f.name)
    try:
        cmd = [
            sys.executable, str(SCRIPT),
            "--manifest", str(MANIFEST),
            "--results", str(results_path),
            "--arm", arm,
            "--epoch", str(epoch),
        ]
        if budget_usd is not None:
            cmd += ["--budget-usd", str(budget_usd)]
        if out_path is not None:
            cmd += ["--out", str(out_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(proc.stdout.strip())
    finally:
        results_path.unlink(missing_ok=True)


def test_resolve_per_dollar_math_on_known_fixture():
    out = _run_cli({
        "t1": {"passed": True, "cost_usd": 0.5},
        "t2": {"passed": False, "cost_usd": 0.3},
        "t3": {"passed": True, "cost_usd": 0.2},
    })
    assert out["arm"] == "off"
    assert out["epoch"] == 0
    assert out["tasks_scored"] == 3
    assert out["resolve_rate"] == pytest.approx(2 / 3)
    assert out["cost_usd"] == pytest.approx(1.0)
    assert out["resolve_per_dollar"] == pytest.approx(2 / 3)
    assert out["budget_usd"] is None


def test_budget_cap_deterministic():
    # Manifest order is [t1, t2, t3] with costs 0.5/0.3/0.2.
    # budget=0.7 → t1 fits (0.5), t2 would push to 0.8 > 0.7 → stop.
    out = _run_cli({
        "t1": {"passed": True, "cost_usd": 0.5},
        "t2": {"passed": True, "cost_usd": 0.3},
        "t3": {"passed": True, "cost_usd": 0.2},
    }, budget_usd=0.7)
    assert out["tasks_scored"] == 1
    assert out["cost_usd"] == pytest.approx(0.5)
    assert out["resolve_rate"] == pytest.approx(1.0)
    assert out["resolve_per_dollar"] == pytest.approx(2.0)


def test_zero_cost_yields_zero_per_dollar():
    out = _run_cli({
        "t1": {"passed": True, "cost_usd": 0.0},
    })
    assert out["cost_usd"] == 0.0
    assert out["resolve_per_dollar"] == 0.0
    assert out["resolve_rate"] == pytest.approx(1.0)
    assert out["tasks_scored"] == 1


def test_arm_and_epoch_passthrough():
    out = _run_cli({"t1": {"passed": True, "cost_usd": 0.5}}, arm="on", epoch=7)
    assert out["arm"] == "on"
    assert out["epoch"] == 7


def test_cli_writes_out_file_when_provided():
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "line.jsonl"
        line = _run_cli(
            {"t1": {"passed": True, "cost_usd": 0.5}},
            out_path=out_path,
        )
        text = out_path.read_text()
        assert text.endswith("\n")
        assert json.loads(text) == line


def test_cli_smoke_matches_documented_contract():
    # Reproduces the cli_smoke contract from the verifier plan verbatim.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({
            "t1": {"passed": True, "cost_usd": 0.5},
            "t2": {"passed": False, "cost_usd": 0.3},
            "t3": {"passed": True, "cost_usd": 0.2},
        }, f)
        results_path = Path(f.name)
    try:
        proc = subprocess.run(
            [
                sys.executable, str(SCRIPT),
                "--manifest", str(MANIFEST),
                "--results", str(results_path),
                "--arm", "off",
                "--epoch", "0",
            ],
            capture_output=True, text=True, check=True,
        )
        out = json.loads(proc.stdout.strip())
        assert out["arm"] == "off"
        assert out["epoch"] == 0
        assert out["resolve_rate"] == pytest.approx(2 / 3)
        assert out["cost_usd"] == pytest.approx(1.0)
        assert out["resolve_per_dollar"] == pytest.approx(2 / 3)
    finally:
        results_path.unlink(missing_ok=True)
