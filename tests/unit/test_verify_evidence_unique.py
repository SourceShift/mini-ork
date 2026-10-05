"""Regression: `mini-ork verify` evidence logs must never collide.

Live smoke of G10-T07 (2026-10-05) ran two `mini-ork verify` dispatches inside
one second. Evidence logs were named `<stem>-<int(time.time())>.log`, so the
second run overwrote the first and the first result's evidence_path pointed at
the other run's REFUTED verdict — an anchoring defect, not a cosmetic one.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mini_ork.cli import verify as ver  # noqa: E402


def _scenario(tmp_path):
    home = tmp_path / ".mini-ork"
    home.mkdir()
    db = str(home / "state.db")
    env = {**os.environ, "MINI_ORK_ENGINE_ROOT": str(REPO), "MINI_ORK_PROJECT_HOME": str(home),
           "MINI_ORK_TARGET_REPO": str(home.parent), "MINI_ORK_ROOT": str(REPO),
           "MINI_ORK_HOME": str(home), "MINI_ORK_DB": db}
    subprocess.run(["bash", str(REPO / "db" / "init.sh")], env=env,
                   capture_output=True, text=True, check=True)
    vdir = home / "verifiers"
    vdir.mkdir()
    (vdir / "goodv.py").write_text('import os\nprint("run", os.environ.get("VT_MARK", ""))\n')
    plan = home / "plan.json"
    plan.write_text(json.dumps({"task_class": "code_fix",
                                "artifact_contract": {"success_verifiers": ["goodv"]}}))
    return home, db, str(plan)


def _verify(home, db, plan, mark):
    o, e = io.StringIO(), io.StringIO()
    old = dict(os.environ)
    os.environ.update({"MINI_ORK_HOME": str(home), "VT_MARK": mark})
    for k in ("MINI_ORK_DRY_RUN", "MINI_ORK_PLAN_PATH", "MINI_ORK_TASK_CLASS", "MINI_ORK_RUN_DIR"):
        os.environ.pop(k, None)
    try:
        with redirect_stdout(o), redirect_stderr(e):
            ver.main(["--plan", plan], db=db, root=str(REPO))
    finally:
        os.environ.clear()
        os.environ.update(old)
    out = json.loads(o.getvalue()[o.getvalue().index("{"):])
    return next(r for r in out["results"] if r["verifier"] == "goodv")["evidence_path"]


def test_same_second_verify_runs_keep_separate_evidence(tmp_path, monkeypatch):
    home, db, plan = _scenario(tmp_path)
    monkeypatch.setattr(ver.time, "time", lambda: 1791224284.0)   # both runs, one second
    first = _verify(home, db, plan, "first")
    second = _verify(home, db, plan, "second")

    assert first != second
    assert Path(first).name.startswith("goodv-1791224284-")       # glob-compatible prefix kept
    assert "first" in Path(first).read_text()                     # not overwritten
    assert "second" in Path(second).read_text()
