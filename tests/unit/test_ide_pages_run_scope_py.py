"""Per-run scope disclosure + the cross-run view (p2-16).

Two things the pages must not get wrong:

* the run page states what the run declared it may touch AND that the
  declaration is not a write sandbox — a scope line that reads like
  enforcement is worse than no line at all;
* the runs page answers the only question a cross-run view exists for — do two
  live runs declare the same file?

Plus the neighbouring truth that made the Scope work necessary: the Spend
section must show the ceiling the dispatch gate actually enforces (the
declared per-run budget), not a different number from the watcher.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from mini_ork.ide_pages import run as run_page
from mini_ork.ide_pages import runs as runs_page
from mini_ork.ide_pages.run import Run


def _mk_run(tmp_path: Path, *, run_id: str = "run-scope-1", status: str = "running",
            cost_total: float | None = 0.84, profile: dict[str, Any] | None = None,
            workspace: Any = None) -> Run:
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if profile is not None:
        (run_dir / "run_profile.json").write_text(json.dumps(profile), encoding="utf-8")
    row: dict[str, Any] = {"id": run_id, "created_at": int(time.time()) - 300,
                           "recipe": "code-fix"}
    card: dict[str, Any] = {"status": status, "recipe": "code-fix", "steps": []}
    if cost_total is not None:
        card["cost_total"] = cost_total
    return Run(id=run_id, home=tmp_path, run_dir=run_dir, row=row, card=card,
               nodes=[], cols=[], calls=[], recipe_dir=None, workspace=workspace,
               edges=[])


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    for var in ("MO_RUN_BUDGET_USD", "MO_WATCH_COST_USD", "MO_ORCH_BUDGET_USD"):
        monkeypatch.delenv(var, raising=False)


def _blob(section: Any) -> str:
    return json.dumps(section, default=str, ensure_ascii=False)


_PROFILE = {
    "task_class": "code-fix",
    "roots": {"target": "/repo/mini-ork", "exec_cwd": "/repo/mini-ork"},
    "scope_allow": ["`mini_ork/dispatch/cost_pause.py`, `mini_ork/cli/execute.py`"],
    "scope_deny": ["Do NOT touch `db/migrations/`"],
}


# ── the run page ───────────────────────────────────────────────────────────


def test_declared_scope_normalizes_the_kickoff_bullets(tmp_path):
    run = _mk_run(tmp_path, profile=_PROFILE)
    scope = run_page.declared_scope(run.run_dir)
    assert scope["target"] == "/repo/mini-ork"
    assert scope["paths"] == ["mini_ork/dispatch/cost_pause.py", "mini_ork/cli/execute.py"]
    assert scope["deny"] == ["Do NOT touch `db/migrations/`"]


def test_scope_section_states_what_it_is_and_is_not(tmp_path):
    run = _mk_run(tmp_path, profile=_PROFILE)
    blob = _blob(run_page._scope_section(run))
    assert "mini_ork/dispatch/cost_pause.py" in blob
    assert "/repo/mini-ork" in blob
    assert "not a write sandbox" in blob
    assert "db/migrations/" in blob


def test_scope_section_says_so_when_nothing_is_declared(tmp_path):
    run = _mk_run(tmp_path)  # no run_profile.json at all
    blob = _blob(run_page._scope_section(run))
    assert "No scope declared" in blob


def test_scope_section_names_the_worktree_branch(tmp_path):
    class _WS:
        branch = "wt/run-scope"
        base_branch = "main"
        path = Path("/tmp/wt")

    run = _mk_run(tmp_path, profile=_PROFILE, workspace=_WS())
    assert "wt/run-scope" in _blob(run_page._scope_section(run))


def test_the_overview_tab_carries_the_scope_section(tmp_path, monkeypatch):
    run = _mk_run(tmp_path, profile=_PROFILE)
    # ``monkeypatch.setattr``, never a bare assignment: a module attribute set
    # directly is not undone at teardown, so every later ``run_page.build`` in
    # the session — in any test file — would answer with THIS run.
    monkeypatch.setattr(run_page, "_load",
                        lambda home, run_id, with_files=True: run)
    page = run_page.build(run.home, "overview", {"run": run.id})
    titles = [s.get("title") for s in page["sections"]]
    assert "Scope" in titles


# ── the spend section reads the ceiling the gate enforces ──────────────────


def test_the_declared_budget_wins_over_the_watcher_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_WATCH_COST_USD", "1.00")
    run = _mk_run(tmp_path, profile={"budget_usd": 12})
    blob = _blob(run_page._spend_section(run))
    assert "$12.00" in blob and "$1.00" not in blob


def test_without_a_declared_budget_the_watcher_cap_stands(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_WATCH_COST_USD", "1.00")
    run = _mk_run(tmp_path, profile={"task_class": "code-fix"})
    assert "$1.00" in _blob(run_page._spend_section(run))


def test_a_parked_run_says_so_in_the_spend_section(tmp_path, monkeypatch):
    from mini_ork.cost import budget

    monkeypatch.setenv("MO_RUN_BUDGET_USD", "0.5")
    run = _mk_run(tmp_path, profile={})
    budget.record(run.run_dir, budget.Verdict(
        spent=0.84, budget=0.5, exhausted=True, detail="spent $0.84 of a $0.50 budget"))
    blob = _blob(run_page._spend_section(run))
    assert "Parked" in blob and "MO_RUN_BUDGET_USD" in blob


def test_a_run_that_fits_again_has_no_parked_line(tmp_path, monkeypatch):
    monkeypatch.setenv("MO_WATCH_COST_USD", "1.00")
    run = _mk_run(tmp_path, profile={})
    assert "Parked" not in _blob(run_page._spend_section(run))


# ── the cross-run view ─────────────────────────────────────────────────────


class _WS:
    def __init__(self, run_id: str, branch: str = "wt/x") -> None:
        self.run_id = run_id
        self.branch = branch
        self.base_branch = "main"
        self.path = Path("/tmp/wt") / run_id


def _open(monkeypatch, *ws) -> None:
    from mini_ork import workspaces

    monkeypatch.setattr(workspaces, "list_open", lambda home: list(ws))


def test_two_live_runs_on_the_same_file_are_flagged(tmp_path, monkeypatch):
    _mk_run(tmp_path, run_id="run-a", profile={
        "scope_allow": ["`mini_ork/a.py`", "`mini_ork/shared.py`"]})
    _mk_run(tmp_path, run_id="run-b", profile={
        "scope_allow": ["`mini_ork/b.py`", "`mini_ork/shared.py`"]})
    _open(monkeypatch, _WS("run-a"), _WS("run-b"))

    blob = _blob(runs_page._scope(tmp_path))
    assert "claimed by more than one live run" in blob
    assert "mini_ork/shared.py" in blob
    assert "run-a · run-b" in blob or "run-b · run-a" in blob


def test_disjoint_scopes_raise_no_callout(tmp_path, monkeypatch):
    _mk_run(tmp_path, run_id="run-a", profile={"scope_allow": ["`mini_ork/a.py`"]})
    _mk_run(tmp_path, run_id="run-b", profile={"scope_allow": ["`mini_ork/b.py`"]})
    _open(monkeypatch, _WS("run-a"), _WS("run-b"))
    blob = _blob(runs_page._scope(tmp_path))
    assert "claimed by more than one live run" not in blob
    assert "2 files" not in blob  # each run declares one
    assert '"t": "1 file"' in blob


def test_no_open_worktree_is_a_quiet_answer(tmp_path, monkeypatch):
    _open(monkeypatch)
    blob = _blob(runs_page._scope(tmp_path))
    assert "No open worktrees" in blob


def test_the_scope_tab_is_registered():
    assert ("scope", "Scope & claims") in runs_page.TABS
    keys = {k for k, _ in runs_page.TABS}
    assert "scope" in keys


def test_a_broken_workspace_source_does_not_blank_the_tab(tmp_path, monkeypatch):
    from mini_ork import workspaces

    def _boom(home):
        raise RuntimeError("record store unreadable")

    monkeypatch.setattr(workspaces, "list_open", _boom)
    assert "No open worktrees" in _blob(runs_page._scope(tmp_path))
