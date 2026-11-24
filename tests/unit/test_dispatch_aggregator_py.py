"""The aggregator recomputes a deliverable's status from a child verdict file.

That check is only independent if the file it cites actually decides
something. `execute` writes a run-level `verdict.json` with
`verdict: "fail"` whenever any node failed, and never rewrites it once a panel
verdict exists — so a child that failed tier4, exited, and was then recovered
is cited by bookkeeping that predates the recovery. Recomputing from that file
reproduced the dispatcher's stale failure instead of checking it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGGREGATOR = ROOT / "recipes" / "spec-driven-delivery" / "verifiers" / "dispatch-aggregator.py"


def _load_aggregator():
    spec = importlib.util.spec_from_file_location("dispatch_aggregator", AGGREGATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


agg = _load_aggregator()


def _record(child: Path, verdict_name: str) -> dict:
    return {
        "child_run_id": child.name,
        "child_run_dir": str(child),
        "verdict_path": str(child / verdict_name),
    }


BOOKKEEPING = {"verdict": "fail", "failed_nodes": 6, "dispatched": 15,
               "source": "execute@run-level"}


def test_check_child_grades_the_panel_when_the_cited_artifact_decides_nothing(tmp_path):
    child = tmp_path / "run--S1--D1"
    child.mkdir()
    (child / "verdict.json").write_text(json.dumps(BOOKKEEPING), encoding="utf-8")
    (child / "panel-verdict.json").write_text(json.dumps({"verdict": "APPROVE"}),
                                              encoding="utf-8")

    status, reason, _commit = agg.check_child(_record(child, "verdict.json"))

    assert status == "delivered", reason
    assert "panel artifact" in reason


def test_check_child_leaves_a_genuine_failure_failed(tmp_path):
    """A roll-up with no panel gate beside it still stands: nothing recovered."""
    child = tmp_path / "run--S1--D1"
    child.mkdir()
    (child / "verdict.json").write_text(json.dumps(BOOKKEEPING), encoding="utf-8")

    status, _reason, _commit = agg.check_child(_record(child, "verdict.json"))

    assert status == "failed"


def test_check_child_keeps_the_cited_artifact_when_it_does_decide(tmp_path):
    """A cited verdict that decides must win — the panel is only a fallback."""
    child = tmp_path / "run--S1--D1"
    child.mkdir()
    (child / "verdict.json").write_text(json.dumps({"pass": True}), encoding="utf-8")
    (child / "panel-verdict.json").write_text(json.dumps({"verdict": "REQUEST_CHANGES"}),
                                              encoding="utf-8")

    status, _reason, _commit = agg.check_child(_record(child, "verdict.json"))

    assert status == "delivered"


def test_check_child_does_not_reread_the_panel_when_it_is_the_cited_artifact(tmp_path):
    child = tmp_path / "run--S1--D1"
    child.mkdir()
    (child / "panel-verdict.json").write_text(json.dumps({"verdict": "ESCALATE"}),
                                              encoding="utf-8")

    status, _reason, _commit = agg.check_child(_record(child, "panel-verdict.json"))

    assert status == "failed"
