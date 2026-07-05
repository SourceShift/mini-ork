"""Parity gate: mini_ork.ported.mini_ork_classify vs bin/mini-ork-classify.

A fixture home with config/task_classes/*.yaml is classified through the LIVE
bash router and the port; the emitted task_class must match across kickoffs that
hit different classes, plus --task-class force, --dry-run, and the DoS-guard.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.ported import mini_ork_classify as cls  # noqa: E402

BIN = REPO / "bin" / "mini-ork-classify"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / ".mini-ork"; (h / "config" / "task_classes").mkdir(parents=True)
    (h / "config" / "task_classes" / "code_fix.yaml").write_text(
        "matches:\n  keywords: [bug, fix, regression, failing test]\n  regex: ['stack ?trace']\n")
    (h / "config" / "task_classes" / "research_synthesis.yaml").write_text(
        "matches:\n  keywords: [literature, survey, arxiv, SOTA]\n")
    # a bare recipes dir so the recipe scan finds nothing extra
    (h / "recipes").mkdir()
    return h


def _class_of(out: str):
    m = re.search(r"task_class=(\S+)", out)
    return m.group(1) if m else None


def _bash(home, kickoff, *args):
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": str(home),
           "MINI_ORK_DB": str(home / "state.db")}
    r = subprocess.run(["bash", str(BIN), str(kickoff), *args], capture_output=True, text=True, env=env)
    return r.stdout, r.returncode


def _py(home, kickoff, *args):
    import io
    from contextlib import redirect_stdout, redirect_stderr
    o, e = io.StringIO(), io.StringIO()
    old = dict(os.environ)
    os.environ.update({"MINI_ORK_HOME": str(home)})
    for k in ("MINI_ORK_DRY_RUN", "MINI_ORK_RUN_ID", "MINI_ORK_RECIPE"):
        os.environ.pop(k, None)
    try:
        with redirect_stdout(o), redirect_stderr(e):
            rc = cls.main([str(kickoff), *args], db=str(home / "state.db"), root=str(REPO))
    finally:
        os.environ.clear(); os.environ.update(old)
    return o.getvalue(), rc


@pytest.mark.parametrize("text,expected", [
    ("This has a nasty bug and a failing test; here is the stack trace.", "code_fix"),
    ("A literature survey of arxiv SOTA methods.", "research_synthesis"),
    ("Totally unrelated content about gardening.", "generic"),
])
def test_classify_parity(tmp_path, home, text, expected):
    k = tmp_path / "kickoff.md"; k.write_text(text)
    ob, rb = _bash(home, k, "--dry-run")
    op, rp = _py(home, k, "--dry-run")
    assert rb == rp == 0
    assert _class_of(ob) == _class_of(op) == expected


def test_force_task_class_parity(tmp_path, home):
    k = tmp_path / "k.md"; k.write_text("has a bug")   # would classify code_fix
    ob, _ = _bash(home, k, "--task-class", "custom_thing", "--dry-run")
    op, _ = _py(home, k, "--task-class", "custom_thing", "--dry-run")
    assert _class_of(ob) == _class_of(op) == "custom_thing"


def test_missing_kickoff_and_dos_guard(tmp_path, home):
    # missing file → rc 2 on both
    assert _bash(home, tmp_path / "nope.md")[1] == _py(home, tmp_path / "nope.md")[1] == 2
    # oversized kickoff → rc 2 on both
    big = tmp_path / "big.md"; big.write_text("x" * 2048)
    env = {**os.environ, "MINI_ORK_ROOT": str(REPO), "MINI_ORK_HOME": str(home),
           "MINI_ORK_DB": str(home / "state.db"), "MO_MAX_KICKOFF_BYTES": "1024"}
    rb = subprocess.run(["bash", str(BIN), str(big)], capture_output=True, text=True, env=env).returncode
    os.environ["MO_MAX_KICKOFF_BYTES"] = "1024"
    try:
        rp = cls.main([str(big)], db=str(home / "state.db"), root=str(REPO))
    finally:
        del os.environ["MO_MAX_KICKOFF_BYTES"]
    assert rb == rp == 2
