"""Parity gate: mini_ork.ported.mini_ork_traceotter vs bin/mini-ork-traceotter.

The distill step needs the TraceOtter venv + real runs, so full render parity is
an integration concern; here we compare the deterministic preflight exit codes
vs live bash, and unit-check the render functions (transcribed verbatim) against
a fixture OUT dir.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_traceotter as tot  # noqa: E402

BIN = REPO / "bin" / "mini-ork-traceotter"
_REAL_PY = Path("/Volumes/docker-ssd/ps/TraceOtter/.venv/bin/python")


def _bash(env_extra, mode=None):
    env = {**os.environ, **env_extra}
    args = ["bash", str(BIN)] + ([mode] if mode else [])
    return subprocess.run(args, capture_output=True, text=True, env=env).returncode


def test_missing_traceotter_venv_parity(tmp_path):
    env = {"TRACEOTTER_HOME": str(tmp_path / "nope"), "MINI_ORK_HOME": str(tmp_path / ".mini-ork")}
    rb = _bash(env)
    old = dict(os.environ); os.environ.update(env)
    try:
        rp = tot.main([])
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rb == rp == 2


def test_missing_runs_parity(tmp_path):
    if not (_REAL_PY.is_file() and os.access(_REAL_PY, os.X_OK)):
        import pytest
        pytest.skip("TraceOtter venv not present")
    home = tmp_path / ".mini-ork"; home.mkdir()   # no runs/ subdir
    env = {"MINI_ORK_HOME": str(home)}             # default TRACEOTTER_HOME (real)
    rb = _bash(env)
    old = dict(os.environ); os.environ.update(env)
    try:
        rp = tot.main([])
    finally:
        os.environ.clear(); os.environ.update(old)
    assert rb == rp == 2


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
