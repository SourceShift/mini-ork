"""The header says how a finished run landed (published-chip).

The delivery bar already names it, but only on the tab that draws the bar: a
reader on the Graph or Kickoff tab sees `published` and cannot tell whether the
run committed, handed over a patch, or left a branch for a person to merge.
The chip carries that distinction on every tab.

Three deliveries, three chips, and one honest absence: a run that delivered
nothing (or is still in flight) grows no chip at all.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from mini_ork import workspaces
from mini_ork.ide_pages import run as run_page
from mini_ork.ide_pages import run_flow
from mini_ork.ide_pages.run import Run

PUBLISH_LINE = "[publish] committed 2 file(s): 1a2b3c4d5e6f\n"
ARTIFACT_LINE = "[ok] publisher: preserved run-local artifact framework-edit.diff\n"


def _mk_run(tmp_path: Path, *, status: str, log: str = "") -> Run:
    run_id = f"run-{status}-1"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if log:
        (run_dir / "execute.log").write_text(log, encoding="utf-8")
    row: dict[str, Any] = {"id": run_id, "created_at": int(time.time()) - 300}
    card: dict[str, Any] = {"status": status, "recipe": "code-fix", "steps": []}
    return Run(id=run_id, home=tmp_path, run_dir=run_dir, row=row, card=card,
               nodes=[], cols=[], calls=[], recipe_dir=None, workspace=None,
               edges=[])


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    for var in ("MO_WATCH_COST_USD", "MO_RUN_BUDGET_USD"):
        monkeypatch.delenv(var, raising=False)


def test_a_committed_run_names_its_short_sha(tmp_path):
    chip = run_page._delivery_chip(_mk_run(tmp_path, status="published", log=PUBLISH_LINE))
    assert chip == {"t": "committed 1a2b3c4", "c": "green"}


def test_a_patch_delivery_says_it_is_proposed_not_applied(tmp_path):
    chip = run_page._delivery_chip(_mk_run(tmp_path, status="published", log=ARTIFACT_LINE))
    assert chip == {"t": "proposed diff", "c": "blue"}


def test_a_published_run_with_real_worktree_work_asks_for_a_merge(tmp_path, monkeypatch):
    run = _mk_run(tmp_path, status="published")
    (run.run_dir / "run_profile.json").write_text(
        json.dumps({"roots": {"target": str(tmp_path / "target")}}), encoding="utf-8")
    run.workspace = object()  # a live workspace, shape irrelevant to the status probe
    monkeypatch.setattr(workspaces, "status",
                        lambda ws: {"exists": True, "commits_ahead": 2,
                                    "uncommitted": False})
    assert run_page._delivery_chip(run) == {"t": "worktree to merge", "c": "yellow"}


def test_a_running_run_grows_no_delivery_chip(tmp_path):
    # In flight: delivery is not the run's outcome yet, and finding it may cost
    # a `git log` in the target repo — never paid on an in-flight page.
    assert run_page._delivery_chip(
        _mk_run(tmp_path, status="running", log=PUBLISH_LINE)) is None


def test_a_published_run_that_delivered_nothing_grows_no_chip(tmp_path):
    assert run_page._delivery_chip(_mk_run(tmp_path, status="published")) is None


def test_the_bar_and_the_chip_read_one_definition(tmp_path):
    """One evidence read, so the bar's ``kind`` and the chip cannot disagree."""
    run = _mk_run(tmp_path, status="published", log=PUBLISH_LINE)
    assert run_flow.delivery(run) == ("commit", "1a2b3c4d5e6f")
    bar = run_flow._merge(run, None, "approved")
    assert bar is not None and bar["kind"] == "commit"


def test_the_v2_header_carries_the_delivery_chip(tmp_path, monkeypatch):
    run = _mk_run(tmp_path, status="published", log=PUBLISH_LINE)
    # monkeypatch, never a bare assignment: an un-restored module attribute
    # would answer every later ``build`` in the session with THIS run.
    monkeypatch.setattr(run_page, "_load",
                        lambda home, run_id, with_files=True: run)
    page = run_page.build(run.home, "graph", {"run": run.id})
    assert {"t": "committed 1a2b3c4", "c": "green"} in page["chips"]
