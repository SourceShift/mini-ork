"""``mini_ork.ide_pages.run_dock`` — the right dock panel's five tabs."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.ide_pages import build_page
from mini_ork.ide_pages import run as run_page
from mini_ork.ide_pages import run_dock
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
         calls: list[dict] | None = None, row: dict | None = None) -> Run:
    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    return Run(id="run-1", home=home, run_dir=run_dir, row=row or {}, card={},
               nodes=nodes, cols=cols, calls=list(calls or []),
               recipe_dir=None, workspace=None)


def _tab(run: Run, tab: str) -> list[dict]:
    return run_dock.build(run, tab)


def _section(sections: list[dict], stype: str, title: str | None = None) -> dict:
    for s in sections:
        if s["type"] == stype and (title is None or s["title"] == title):
            return s
    raise AssertionError(f"no {stype} section (title={title!r}) in "
                         f"{[(s['type'], s['title']) for s in sections]}")


# ── changes ──────────────────────────────────────────────────────────────────

def test_changes_tab_lists_files_with_counts(tmp_path: Path) -> None:
    nodes = [Node(id="implementer", type="implementer", state="done", start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["implementer"]])
    (run.run_dir / "framework-edit.diff").write_text(_PATCH)

    sections = _tab(run, "changes")
    summary = _section(sections, "triage")
    assert "+2 −1 across 1 file" in summary["text"]
    assert summary["full"] is False  # one column, no full-width layout
    files = _section(sections, "files", "Changed files")
    entry = files["files"][0]
    assert entry["path"] == "mini_ork/foo.py" and entry["added"] == 2 and entry["removed"] == 1
    assert "+b" in files["diff"]
    assert files["full"] is False


def test_changes_tab_empty_state(tmp_path: Path) -> None:
    nodes = [Node(id="implementer", type="implementer", state="done", start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["implementer"]])

    sections = _tab(run, "changes")
    assert _section(sections, "triage")["text"].startswith("+0 −0 across 0 files")
    empty = _section(sections, "list", "Changed files")
    assert empty["items"][0]["t"] == "No changes recorded for this run"


# ── checks ───────────────────────────────────────────────────────────────────

def test_checks_tab_failing_check_row(tmp_path: Path) -> None:
    nodes = [Node(id="test_verifier", type="verifier", state="failed", start=T0, end=T0 + 5,
                  finish="fail")]
    run = _run(tmp_path, nodes, [["test_verifier"]])
    (run.run_dir / "verifier_test_verifier.json").write_text(json.dumps({
        "verifier": "test_verifier",
        "checks": [{"name": "pytest", "pass": False, "rc": 1}],
    }))
    (run.run_dir / "verifier_test_verifier.log").write_text("line 1\nFAILED tests/x.py::test_a\n")

    sections = _tab(run, "checks")
    assert _section(sections, "triage")["menu"] == []  # the dock shows no menu
    section = _section(sections, "checks", "test_verifier")
    assert section["summary"]["failing"] == 1
    row = section["rows"][0]
    assert row["name"] == "pytest" and row["state"] == "fail"
    assert row["log"]  # a failing row carries up to 8 log lines


def test_checks_tab_reviewer_findings(tmp_path: Path) -> None:
    nodes = [Node(id="reviewer", type="reviewer", state="done", start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["reviewer"]])
    (run.run_dir / "review-reviewer.json").write_text(json.dumps({
        "verdict": "needs_revision",
        "findings": [{"issue": "off-by-one", "severity": "medium",
                      "file": "mini_ork/foo.py", "line": 12}],
    }))

    sections = _tab(run, "checks")
    findings = _section(sections, "findings", "Review · reviewer")
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

    section = _section(_tab(run, "checks"), "checks", "Levels")
    states = {r["name"]: r["state"] for r in section["rows"]}
    assert states["applies"] == "pass"
    assert states["executes"] == "fail"
    assert states["target"] == "pending"
    assert states["contract"] == "na"
    assert section["summary"] == {"passing": 1, "failing": 1, "pending": 1, "na": 1}


# ── agents ───────────────────────────────────────────────────────────────────

def test_agents_tab_rows_in_cols_order(tmp_path: Path) -> None:
    nodes = [
        Node(id="planner", type="planner", state="done", start=T0, end=T0 + 5, family="glm"),
        Node(id="implementer", type="implementer", state="done", start=T0 + 5, end=T0 + 40,
             family="minimax", cost=0.07),
        Node(id="publisher", type="publisher", state="pending", family="shell"),
    ]
    run = _run(tmp_path, nodes, [["planner"], ["implementer"], ["publisher"]])

    rows = _section(_tab(run, "agents"), "agents", "Pipeline")["rows"]
    assert [r["id"] for r in rows] == ["planner", "implementer", "publisher"]
    impl = rows[1]
    assert impl["lane"] == "minimax"
    assert impl["cost"] == "$0.07"
    assert impl["dur"] == "35s"
    assert impl["do"] == {"page": "run", "tab": "graph",
                          "args": {"run": "run-1", "node": "implementer"}}


# ── cost ─────────────────────────────────────────────────────────────────────

def test_cost_tab_by_lane_bars_sum_to_the_run_total(tmp_path: Path) -> None:
    nodes = [
        Node(id="planner", type="planner", state="done", start=T0, end=T0 + 5,
             family="glm", cost=0.2),
        Node(id="implementer", type="implementer", state="done", start=T0 + 5, end=T0 + 40,
             family="minimax", cost=0.3),
    ]
    run = _run(tmp_path, nodes, [["planner"], ["implementer"]], row={"cost_usd": 0.5})

    sections = _tab(run, "cost")
    assert _section(sections, "kv", "Spend")["items"][0]["v"] == "$0.50"
    lanes = _section(sections, "bars", "By lane")
    total = sum(float(item["val"].lstrip("$")) for item in lanes["items"])
    assert total == pytest.approx(run_page._cost(run), abs=0.01)
    assert {item["label"] for item in lanes["items"]} == {"glm", "minimax"}
    assert all(item["c"].startswith("fam:") for item in lanes["items"])


# ── learned ──────────────────────────────────────────────────────────────────

def test_learned_tab_injected_node(tmp_path: Path) -> None:
    nodes = [Node(id="implementer", type="implementer", state="done", start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["implementer"]])
    learned = run.run_dir / "learned"
    learned.mkdir()
    (learned / "implementer.json").write_text(json.dumps({
        "injected": True,
        "sources": [{"kind": "gradient"}, {"kind": "pattern"}, {"kind": "steering"}],
    }))
    (learned / "implementer.md").write_text("# what was injected\n")

    item = _section(_tab(run, "learned"), "list", "Learned")["items"][0]
    assert item["m"] == "✓" and item["mc"] == "green"
    assert item["t"] == "implementer"
    assert item["sub"] == "injected · 3 sources (gradient, pattern, steering)"
    assert item["acts"][0]["do"]["path"].endswith("implementer.md")


def test_learned_tab_empty_state(tmp_path: Path) -> None:
    nodes = [Node(id="publisher", type="publisher", state="done", start=T0, end=T0 + 60)]
    run = _run(tmp_path, nodes, [["publisher"]])
    item = _section(_tab(run, "learned"), "list", "Learned")["items"][0]
    assert item["t"] == "No learned context was recorded for this run"


# ── fail-soft + read-only ────────────────────────────────────────────────────

def test_a_broken_section_is_guarded(tmp_path: Path, monkeypatch) -> None:
    nodes = [Node(id="publisher", type="publisher", state="done", start=T0, end=T0 + 60)]
    run = _run(tmp_path, nodes, [["publisher"]])

    def boom(_run):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(run_dock, "_pipeline", boom)
    errors: dict[str, str] = {}
    sections = run_dock.build(run, "agents", errors)
    assert sections[0]["type"] == "list" and sections[0]["title"] == "Pipeline"
    assert "kaboom" in errors["Pipeline"]


def test_all_five_tabs_are_read_only(tmp_path: Path) -> None:
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
    (run.run_dir / "learned").mkdir()
    (run.run_dir / "learned" / "implementer.json").write_text(json.dumps({"injected": False}))

    def snap() -> dict[str, bytes]:
        return {str(p.relative_to(run.run_dir)): p.read_bytes()
                for p in run.run_dir.rglob("*") if p.is_file()}

    before = snap()
    for tab in ("changes", "checks", "agents", "cost", "learned"):
        run_dock.build(run, tab)
    assert snap() == before


# ── the page branch ──────────────────────────────────────────────────────────

def _iso(epoch: int) -> str:  # noqa: D401 - fixture helper mirroring the run tests
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


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


def test_dock_tab_bar_at_level_two(home: Path, monkeypatch) -> None:
    _seed(home)
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = build_page(home, "run", None, {"run": RUN, "view": "dock"})

    assert [t["key"] for t in page["tabs"]] == [
        "changes", "checks", "agents", "cost", "learned"]
    assert page["tab"] == "changes"  # the default tab
    assert page["args"] == {"run": RUN, "view": "dock"}
    assert page["chips"] == [] and page["actions"] == []
    assert "graph" in page  # the same top-level graph the run page carries

    # An unknown tab resolves to ``changes``.
    assert build_page(home, "run", "nope", {"run": RUN, "view": "dock"})["tab"] == "changes"


def test_without_the_spec_is_todays_page(home: Path, monkeypatch) -> None:
    _seed(home)
    monkeypatch.delenv("MINI_ORK_IDE_SPEC", raising=False)
    page = build_page(home, "run", None, {"run": RUN, "view": "dock"})
    assert page["tab"] == "dag"
    assert [t["key"] for t in page["tabs"]] == [
        "dag", "kickoff", "overview", "agents", "learnings", "artifacts"]


def test_level_two_without_view_is_the_story(home: Path, monkeypatch) -> None:
    _seed(home)
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = build_page(home, "run", None, {"run": RUN})
    assert page["tab"] == "story"
    assert page["sections"][0]["type"] == "triage"


def test_last_line_splits_a_multi_line_entry() -> None:
    """An agent result entry can hold many lines; the row shows only the last one."""
    from mini_ork.ide_pages import run_dock

    class _Run:  # minimal stand-ins: only _node_output is consulted
        pass

    import mini_ork.ide_pages.run as R

    orig = R._node_output
    try:
        R._node_output = lambda run, node: ["first", "Implementation complete.\n\n## What I built\nthe end\n"]
        assert run_dock._last_line(_Run(), object()) == "the end"
    finally:
        R._node_output = orig
