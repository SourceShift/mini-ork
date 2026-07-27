"""Unit tests: mini_ork.cli.traceotter (bash parity halves removed; formerly vs bin/mini-ork-traceotter).

The distill step needs the TraceOtter venv + real runs, so full render is an
integration concern; here we assert the deterministic preflight exit codes and
unit-check the render functions against a fixture OUT dir.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import traceotter as tot

_REAL_PY = Path("/Volumes/docker-ssd/ps/TraceOtter/.venv/bin/python")


def test_missing_traceotter_venv(tmp_path):
    env = {"TRACEOTTER_HOME": str(tmp_path / "nope"), "MINI_ORK_HOME": str(tmp_path / ".mini-ork")}
    old = dict(os.environ); os.environ.update(env)
    try:
        rp = tot.main([])
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rp == 2


def test_missing_runs(tmp_path):
    if not (_REAL_PY.is_file() and os.access(_REAL_PY, os.X_OK)):
        import pytest
        pytest.skip("TraceOtter venv not present")
    home = tmp_path / ".mini-ork"; home.mkdir()   # no runs/ subdir
    env = {"MINI_ORK_HOME": str(home)}             # default TRACEOTTER_HOME (real)
    old = dict(os.environ); os.environ.update(env)
    try:
        rp = tot.main([])
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rp == 2


def _fixture_out(tmp_path):
    out = tmp_path / "traceotter"; out.mkdir()
    eps = [
        {"outcome": {"status": "completed", "costUsd": 1.5, "toolCalls": 10, "toolErrors": 1,
                     "testsPassed": True}, "labels": {"processScore": 0.6, "shouldImitate": True}},
        {"outcome": {"status": "partial", "costUsd": 0.5, "toolCalls": 4, "toolErrors": 0,
                     "testsPassed": None}, "labels": {"processScore": 0.4, "shouldImitate": False}},
    ]
    (out / "episodes.jsonl").write_text("\n".join(json.dumps(e) for e in eps))
    (out / "report.json").write_text(json.dumps({"skills": 7,
        "llamafactory": {"examples": "42", "dataset": "/d/ds.json", "train_command": "llamafactory-cli train x"}}))
    (out / "skills.json").write_text(json.dumps([
        {"skillId": "sk1", "support": 9, "procedure": ["do a", "then b"]},
        {"skillId": "sk2", "sourceEpisodeIds": ["e1"], "procedure": ["x"]}]))
    return str(out)


def test_render_analytics_grounded(tmp_path):
    out = _fixture_out(tmp_path)
    s = tot.render_analytics(out, "runs")
    assert "$2.00" in s                       # real cost sum, formatted
    assert "92.9%" in s                       # tool reliability: 14 calls, 1 err → 100*(1-1/14)
    assert "completed 1 · partial 1 · failed 0" in s
    assert "distilled skills  7" in s and "42 examples" in s
    assert "clean-imitate     1" in s         # one shouldImitate


def test_render_skills_and_dataset(tmp_path):
    out = _fixture_out(tmp_path)
    sk = tot.render_skills(out)
    assert "2 distilled skills" in sk and "sk1" in sk and "do a → then b" in sk
    ds = tot.render_dataset(out)
    assert "SFT examples: 42" in ds and "llamafactory-cli train x" in ds
