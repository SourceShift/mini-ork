"""``mini_ork.ide_pages.run_dock`` — the right dock's one glance column.

The dock is a summary plus doors, not a second run page: every fact it draws is
either short (the outcome triage, ``+a −r``, spend) or the one thing worth
reading without a click (a failing check with its log tail). The tabs it used
to carry — Agents, Cost, Learned — live on the run page, and the dock points at
them instead.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_ork.ide_pages import build_page
from mini_ork.ide_pages import run as run_page
from mini_ork.ide_pages import run_dock
from mini_ork.ide_pages import spec as S
from mini_ork.ide_pages.run import Node, Run
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000000-abc123"
T0 = 1_791_000_000

WORKFLOW = """\
version: 1
task_class: demo
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md}
  - {name: test_verifier, type: verifier, verifier_ref: verifiers/test.py}
  - {name: reviewer, type: reviewer, model_lane: reviewer, prompt_ref: prompts/reviewer.md}
  - {name: publisher, type: publisher}
edges:
  - {from: planner, to: implementer, edge_type: depends_on}
  - {from: implementer, to: test_verifier, edge_type: verifies}
  - {from: test_verifier, to: reviewer, edge_type: depends_on}
  - {from: reviewer, to: publisher, edge_type: depends_on}
"""

_PATCH = """\
diff --git a/mini_ork/foo.py b/mini_ork/foo.py
--- a/mini_ork/foo.py
+++ b/mini_ork/foo.py
@@ -1,2 +1,3 @@
 a
+b
+c
-d
"""


def _run(tmp_path: Path, nodes: list[Node], cols: list[list[str]],
         calls: list[dict] | None = None, row: dict | None = None,
         workspace: object | None = None) -> Run:
    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    return Run(id="run-1", home=home, run_dir=run_dir, row=row or {}, card={},
               nodes=nodes, cols=cols, calls=list(calls or []),
               recipe_dir=None, workspace=workspace)


def _glance(run: Run, errors: dict[str, str] | None = None) -> list[dict]:
    return run_dock.build(run, errors=errors)


def _section(sections: list[dict], stype: str, title: str | None = None) -> dict:
    for s in sections:
        if s["type"] == stype and (title is None or s["title"] == title):
            return s
    raise AssertionError(f"no {stype} section (title={title!r}) in "
                         f"{[(s['type'], s['title']) for s in sections]}")


# ── outcome ──────────────────────────────────────────────────────────────────

def test_the_glance_leads_with_the_one_outcome(tmp_path: Path) -> None:
    nodes = [Node(id="publisher", type="publisher", state="done", start=T0, end=T0 + 60)]
    run = _run(tmp_path, nodes, [["publisher"]])

    sections = _glance(run)
    assert sections[0]["type"] == "triage"
    assert sections[0]["menu"] == []  # the dock shows no menu
    assert sections[0]["full"] is False  # one column, no full-width layout
    # The status appears exactly once: no second triage behind it.
    assert [s["type"] for s in sections].count("triage") == 1


# ── checks ───────────────────────────────────────────────────────────────────

def _verifier_run(tmp_path: Path) -> Run:
    nodes = [Node(id="test_verifier", type="verifier", state="failed", start=T0, end=T0 + 5,
                  finish="fail")]
    run = _run(tmp_path, nodes, [["test_verifier"]])
    (run.run_dir / "verifier_test_verifier.json").write_text(json.dumps({
        "verifier": "test_verifier",
        "checks": [{"name": "pytest", "pass": False, "rc": 1},
                   {"name": "ruff", "pass": True}],
    }))
    (run.run_dir / "verifier_test_verifier.log").write_text("line 1\nFAILED tests/x.py::test_a\n")
    return run


def test_checks_glance_lists_only_the_failing_rows(tmp_path: Path) -> None:
    run = _verifier_run(tmp_path)

    section = _section(_glance(run), "checks", "Checks")
    assert section["summary"] == {"passing": 1, "failing": 1, "pending": 0, "na": 0}
    assert [r["name"] for r in section["rows"]] == ["pytest"]
    row = section["rows"][0]
    assert row["state"] == "fail"
    assert row["log"]  # a failing row carries up to 8 log lines
    action = section["actions"][0]
    assert action["label"] == "Open Checks"
    assert action["do"] == {"page": "run", "tab": "checks", "args": {"run": "run-1"}}


def test_checks_glance_empty_state(tmp_path: Path) -> None:
    nodes = [Node(id="publisher", type="publisher", state="done", start=T0, end=T0 + 60)]
    run = _run(tmp_path, nodes, [["publisher"]])

    section = _section(_glance(run), "list", "Checks")
    assert section["items"][0]["t"] == "No verifier checks recorded for this run"
    assert section["actions"][0]["label"] == "Open Checks"


def test_the_checks_tab_holds_every_row_and_no_triage(tmp_path: Path) -> None:
    run = _verifier_run(tmp_path)

    sections = run_dock.checks_sections(run)
    assert all(s["type"] != "triage" for s in sections)  # Outcome owns the status
    section = _section(sections, "checks", "test_verifier")
    assert [r["name"] for r in section["rows"]] == ["pytest", "ruff"]
    assert section["summary"]["passing"] == 1


def test_checks_tab_reviewer_findings(tmp_path: Path) -> None:
    nodes = [Node(id="reviewer", type="reviewer", state="done", start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["reviewer"]])
    (run.run_dir / "review-reviewer.json").write_text(json.dumps({
        "verdict": "needs_revision",
        "findings": [{"issue": "off-by-one", "severity": "medium",
                      "file": "mini_ork/foo.py", "line": 12}],
    }))

    findings = _section(run_dock.checks_sections(run), "findings", "Review · reviewer")
    assert findings["verdict"] == {"t": "needs_revision", "c": "yellow"}
    finding = findings["items"][0]
    assert finding["issue"] == "off-by-one" and finding["file"] == "mini_ork/foo.py"


def test_checks_tab_levels(tmp_path: Path) -> None:
    nodes = [Node(id="publisher", type="publisher", state="done", start=T0, end=T0 + 60)]
    run = _run(tmp_path, nodes, [["publisher"]])
    (run.run_dir / "run-verdict.json").write_text(json.dumps({
        "levels": {"applies": "PROVEN", "executes": "REFUTED",
                   "target": "UNVERIFIED", "contract": "n/a"},
    }))

    section = _section(run_dock.checks_sections(run), "checks", "Levels")
    states = {r["name"]: r["state"] for r in section["rows"]}
    assert states["applies"] == "pass"
    assert states["executes"] == "fail"
    assert states["target"] == "pending"
    assert states["contract"] == "na"
    assert section["summary"] == {"passing": 1, "failing": 1, "pending": 1, "na": 1}


# ── spend ────────────────────────────────────────────────────────────────────

def test_spend_shows_the_run_total_and_its_cap(tmp_path: Path) -> None:
    nodes = [
        Node(id="planner", type="planner", state="done", start=T0, end=T0 + 5,
             family="glm", cost=0.2),
        Node(id="implementer", type="implementer", state="done", start=T0 + 5, end=T0 + 40,
             family="minimax", cost=0.3),
    ]
    run = _run(tmp_path, nodes, [["planner"], ["implementer"]], row={"cost_usd": 0.5})

    section = _section(_glance(run), "kv", "Spend")
    items = {i["k"]: i["v"] for i in section["items"]}
    assert items["This run"] == S.money(run_page._cost(run))
    assert items["Run budget"] == "no cap"
    assert section["actions"][0]["label"] == "Open Agents"


# ── changes ──────────────────────────────────────────────────────────────────

def test_changes_glance_summarises_files_without_the_diff(tmp_path: Path) -> None:
    nodes = [Node(id="implementer", type="implementer", state="done", start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["implementer"]])
    (run.run_dir / "framework-edit.diff").write_text(_PATCH)

    section = _section(_glance(run), "kv", "Changes")
    items = {i["k"]: i["v"] for i in section["items"]}
    assert items["diff"] == "+2 −1 across 1 file"
    assert "branch" not in items  # no workspace → nothing to say about a branch
    action = section["actions"][-1]
    assert action["label"] == "Open Changes"
    assert action["do"] == {"page": "run", "tab": "artifacts", "args": {"run": "run-1"}}


def test_changes_glance_empty_state(tmp_path: Path) -> None:
    nodes = [Node(id="implementer", type="implementer", state="done", start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["implementer"]])

    items = {i["k"]: i["v"]
             for i in _section(_glance(run), "kv", "Changes")["items"]}
    assert items["diff"] == "+0 −0 across 0 files"


def test_changes_glance_names_the_worktree_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mini_ork import workspaces

    monkeypatch.setattr(workspaces, "status",
                        lambda ws: {"exists": True, "commits_ahead": 2})
    ws = SimpleNamespace(branch="rp-c17", base_branch="main")
    run = _run(tmp_path, [Node(id="implementer", type="implementer", state="running",
                               start=T0)], [["implementer"]], workspace=ws)

    items = {i["k"]: i["v"]
             for i in _section(_glance(run), "kv", "Changes")["items"]}
    assert items["branch"] == "on rp-c17 · 2 commits ahead of main"


# ── more ─────────────────────────────────────────────────────────────────────

def test_more_links_to_the_tabs_the_glance_does_not_summarise(tmp_path: Path) -> None:
    nodes = [Node(id="publisher", type="publisher", state="done", start=T0, end=T0 + 60)]
    run = _run(tmp_path, nodes, [["publisher"]])

    items = _section(_glance(run), "list", "More")["items"]
    assert [i["t"] for i in items] == ["Orchestrator", "Learnings"]
    assert items[0]["acts"][0]["do"] == {
        "page": "run", "tab": "orchestrator", "args": {"run": "run-1"}}


# ── fail-soft + read-only ────────────────────────────────────────────────────

def test_a_broken_section_is_guarded(tmp_path: Path, monkeypatch) -> None:
    nodes = [Node(id="publisher", type="publisher", state="done", start=T0, end=T0 + 60)]
    run = _run(tmp_path, nodes, [["publisher"]])

    def boom(_run):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(run_dock, "_spend", boom)
    errors: dict[str, str] = {}
    sections = _glance(run, errors)
    broken = _section(sections, "list", "Spend")
    assert "kaboom" in errors["Spend"]
    assert broken is not None


def test_the_glance_and_the_checks_tab_are_read_only(tmp_path: Path) -> None:
    nodes = [
        Node(id="implementer", type="implementer", state="done", start=T0, end=T0 + 30,
             family="minimax", cost=0.07),
        Node(id="test_verifier", type="verifier", state="done", start=T0 + 30, end=T0 + 35,
             family="shell"),
        Node(id="reviewer", type="reviewer", state="done", start=T0 + 35, end=T0 + 60,
             family="glm"),
    ]
    run = _run(tmp_path, nodes, [["implementer"], ["test_verifier"], ["reviewer"]])
    (run.run_dir / "framework-edit.diff").write_text(_PATCH)
    (run.run_dir / "verifier_test_verifier.json").write_text(json.dumps({
        "checks": [{"name": "pytest", "pass": True}],
    }))
    (run.run_dir / "review-reviewer.json").write_text(json.dumps({"verdict": "approve"}))

    def snap() -> dict[str, bytes]:
        return {str(p.relative_to(run.run_dir)): p.read_bytes()
                for p in run.run_dir.rglob("*") if p.is_file()}

    before = snap()
    run_dock.build(run)
    run_dock.checks_sections(run)
    assert snap() == before


# ── the page branch ──────────────────────────────────────────────────────────

@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _out, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    recipe = h / "recipes" / "demo-recipe"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(WORKFLOW)
    (recipe / "task_class.yaml").write_text("name: demo\ndescription: a demo recipe\n")
    return h


def _seed(home: Path) -> Path:
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True)
    kickoff.write_text("# Make the demo pass\n\nDetails.\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (RUN, "demo-recipe", "published", 0.5, T0, T0 + 120, T0 + 100, "demo",
         str(kickoff), "latest", "tr-demo-1"))
    con.commit()
    con.close()
    (run_dir / "run_profile.json").write_text(json.dumps(
        {"user_goal": "Ship the demo", "success_criteria": ["it works"]}))
    return run_dir


def test_dock_has_no_tab_bar_at_level_two(home: Path, monkeypatch) -> None:
    _seed(home)
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = build_page(home, "run", None, {"run": RUN, "view": "dock"})

    assert page["tabs"] == []  # one glance column; the run page owns the tabs
    assert page["tab"] == ""
    assert page["args"] == {"run": RUN, "view": "dock"}
    assert page["chips"] == [] and page["actions"] == []
    assert page["sections"][0]["type"] == "triage"
    assert "graph" in page  # the same top-level graph the run page carries


def test_the_run_page_has_a_checks_tab(home: Path, monkeypatch) -> None:
    _seed(home)
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = build_page(home, "run", "checks", {"run": RUN})

    assert page["tab"] == "checks"
    assert "checks" in [t["key"] for t in page["tabs"]]


def test_without_the_spec_is_todays_page(home: Path, monkeypatch) -> None:
    _seed(home)
    monkeypatch.delenv("MINI_ORK_IDE_SPEC", raising=False)
    page = build_page(home, "run", None, {"run": RUN, "view": "dock"})
    assert page["tab"] == "dag"
    assert [t["key"] for t in page["tabs"]] == [
        "dag", "kickoff", "overview", "orchestrator", "agents", "learnings", "artifacts"]
