"""``mini_ork.ide_pages.run_story`` — the run story's step-by-step rows."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mini_ork.ide_pages import run as run_page
from mini_ork.ide_pages import run_story
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
  - {name: code_impact_lens, type: researcher, model_lane: lens, prompt_ref: prompts/lens.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md}
  - {name: test, type: verifier, verifier_ref: verifiers/test.py}
  - {name: reviewer, type: reviewer, model_lane: reviewer, prompt_ref: prompts/reviewer.md}
  - {name: publisher, type: publisher}
edges:
  - {from: planner, to: code_impact_lens, edge_type: depends_on}
  - {from: planner, to: implementer, edge_type: depends_on}
  - {from: implementer, to: test, edge_type: verifies}
  - {from: test, to: reviewer, edge_type: depends_on}
  - {from: reviewer, to: publisher, edge_type: depends_on}
"""


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── a bare Run for the unit-level tests (no DB, no recipe) ───────────────────

def _run(tmp_path: Path, nodes: list[Node], cols: list[list[str]],
         calls: list[dict] | None = None) -> Run:
    home = tmp_path / ".mini-ork"
    run_dir = home / "runs" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    return Run(id="run-1", home=home, run_dir=run_dir, row={}, card={},
               nodes=nodes, cols=cols, calls=list(calls or []),
               recipe_dir=None, workspace=None)


def _step(story: dict, step_id: str) -> dict:
    for s in story["steps"]:
        if s["id"] == step_id:
            return s
    raise AssertionError(f"no step {step_id!r} in {[s['id'] for s in story['steps']]}")


# ── steps, order, states ────────────────────────────────────────────────────

def test_steps_follow_cols_order_and_map_states(tmp_path: Path) -> None:
    nodes = [
        Node(id="planner", type="planner", state="done", start=T0, end=T0 + 5, family="glm"),
        Node(id="implementer", type="implementer", state="done", start=T0 + 5, end=T0 + 40,
             family="minimax", cost=0.07, calls=3),
        Node(id="test", type="verifier", state="done", start=T0 + 40, end=T0 + 45, family="shell"),
        Node(id="reviewer", type="reviewer", state="running", start=T0 + 45, family="glm"),
        Node(id="publisher", type="publisher", state="pending", family="shell"),
        Node(id="rollback", type="rollback", state="pending", family="shell",
             finish="skipped"),
    ]
    cols = [["planner"], ["implementer"], ["test"], ["reviewer"], ["publisher"], ["rollback"]]
    run = _run(tmp_path, nodes, cols)

    story = run_story.story_section(run)
    assert story["type"] == "story" and story["full"] is True
    assert [s["id"] for s in story["steps"]] == [
        "planner", "implementer", "test", "reviewer", "publisher", "rollback"]
    assert _step(story, "planner")["state"] == "done"
    assert _step(story, "reviewer")["state"] == "running"
    assert _step(story, "rollback")["state"] == "skipped"  # finish_reason = skipped
    assert _step(story, "publisher")["state"] == "pending"
    # Headline, lane, meta, dur, do.
    impl = _step(story, "implementer")
    assert impl["lane"] == "minimax"
    assert impl["headline"]["t"] == "no files changed"
    assert impl["meta"] == ["3 calls"]
    assert impl["dur"] == "35s"
    assert impl["cost"] == "$0.07"
    assert impl["do"] == {"page": "run", "tab": "graph",
                          "args": {"run": "run-1", "node": "implementer"}}


def test_a_node_never_started_outside_the_workflow_is_skipped(tmp_path: Path) -> None:
    nodes = [
        Node(id="planner", type="planner", state="done", start=T0, end=T0 + 5),
        Node(id="ghost", type="shell", state="pending", in_workflow=False),
    ]
    run = _run(tmp_path, nodes, [["planner"], ["ghost"]])
    story = run_story.story_section(run)
    assert [s["id"] for s in story["steps"]] == ["planner"]


# ── implementer: files block, first implementer only ────────────────────────

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


def test_first_implementer_gets_the_files_block(tmp_path: Path) -> None:
    nodes = [
        Node(id="implementer", type="implementer", state="done", start=T0, end=T0 + 30),
        Node(id="implementer2", type="implementer", state="done", start=T0 + 30, end=T0 + 60),
    ]
    run = _run(tmp_path, nodes, [["implementer"], ["implementer2"]])
    (run.run_dir / "framework-edit.diff").write_text(_PATCH)

    story = run_story.story_section(run)
    first = _step(story, "implementer")
    blocks = first["body"]
    assert len(blocks) == 1 and blocks[0]["kind"] == "files"
    entry = blocks[0]["files"][0]
    assert entry["path"] == "mini_ork/foo.py" and entry["added"] == 2 and entry["removed"] == 1
    assert "+b" in blocks[0]["diff"]
    assert first["open"] is True  # first implementer opens by default
    assert first["headline"]["t"] == "changed 1 files (+2 −1)"
    # The second implementer shows no files block — the run-cumulative set
    # rides the first one only.
    assert _step(story, "implementer2")["body"] == []
    assert _step(story, "implementer2")["open"] is False


# ── reviewer: findings block + open ─────────────────────────────────────────

def test_reviewer_findings_block_and_open(tmp_path: Path) -> None:
    nodes = [Node(id="reviewer", type="reviewer", state="done", start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["reviewer"]])
    (run.run_dir / "review-reviewer.json").write_text(json.dumps({
        "verdict": "needs_revision",
        "findings": [{"issue": "off-by-one", "severity": "medium",
                      "file": "mini_ork/foo.py", "line": 12}],
    }))

    story = run_story.story_section(run)
    step = _step(story, "reviewer")
    block = step["body"][0]
    assert block["kind"] == "findings"
    assert block["verdict"] == {"t": "needs_revision", "c": "yellow"}
    finding = block["items"][0]
    assert finding["issue"] == "off-by-one"
    assert finding["file"] == "mini_ork/foo.py"
    assert finding["line"] == 12
    assert finding["severity"] == "medium"
    assert finding["source"] == "reviewer"
    # The headline is ``_overview_headline``'s (reused, not re-derived): the
    # verdict, coloured yellow for ``needs_revision``.
    assert step["headline"] == {"t": "needs_revision", "c": "yellow"}
    assert step["open"] is True


def test_reviewer_without_findings_stays_collapsed(tmp_path: Path) -> None:
    # An approve verdict with an empty findings list is not "findings": the
    # kickoff opens the reviewer only when it has some.
    nodes = [Node(id="reviewer", type="reviewer", state="done", start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["reviewer"]])
    (run.run_dir / "review-reviewer.json").write_text(json.dumps({
        "verdict": "approve", "findings": [], "reasons": ["looks good"],
    }))

    step = _step(run_story.story_section(run), "reviewer")
    assert step["open"] is False
    assert step["headline"]["t"].startswith("approve")
    block = step["body"][0]
    assert block["kind"] == "findings" and block["items"] == []
    assert block["reasons"] == ["looks good"]


def test_dict_shaped_notes_become_finding_items(tmp_path: Path) -> None:
    # The shape the runtime asks reviewers for: {verdict, notes: [{file, line,
    # issue}]}. Dict entries are findings, not Python reprs in the reasons list.
    nodes = [Node(id="reviewer", type="reviewer", state="done", start=T0, end=T0 + 30)]
    run = _run(tmp_path, nodes, [["reviewer"]])
    (run.run_dir / "review-reviewer.json").write_text(json.dumps({
        "verdict": "needs_revision",
        "notes": [{"file": "mini_ork/foo.py", "line": 7, "issue": "unused import"},
                  "plus a plain note"],
    }))

    step = _step(run_story.story_section(run), "reviewer")
    block = step["body"][0]
    assert block["kind"] == "findings"
    finding = block["items"][0]
    assert finding["issue"] == "unused import"
    assert finding["file"] == "mini_ork/foo.py" and finding["line"] == 7
    assert block["reasons"] == ["plus a plain note"]
    assert all("{" not in r for r in block["reasons"])
    assert step["open"] is True


def test_lens_without_json_renders_its_report_markdown(tmp_path: Path) -> None:
    nodes = [Node(id="code_impact_lens", type="researcher", state="done", start=T0, end=T0 + 9)]
    run = _run(tmp_path, nodes, [["code_impact_lens"]])
    (run.run_dir / "lens-code_impact.md").write_text("# What it touched\n\n- foo.py\n")

    step = _step(run_story.story_section(run), "code_impact_lens")
    block = step["body"][0]
    assert block["kind"] == "md" and block["text"].startswith("# What it touched")
    assert step["headline"]["t"] == "What it touched"
    assert step["open"] is False


# ── verifier: checks block with a failing row ───────────────────────────────

def test_verifier_failing_check_rows(tmp_path: Path) -> None:
    nodes = [Node(id="test", type="verifier", state="failed", start=T0, end=T0 + 5,
                  finish="fail")]
    run = _run(tmp_path, nodes, [["test"]])
    (run.run_dir / "verifier_test.json").write_text(json.dumps({
        "verifier": "test",
        "checks": [{"name": "pytest", "pass": False, "rc": 1}],
    }))
    (run.run_dir / "verifier_test.log").write_text("line 1\nFAILED tests/x.py::test_a\n")

    step = _step(run_story.story_section(run), "test")
    block = step["body"][0]
    assert block["kind"] == "checks"
    assert block["summary"]["failing"] == 1
    row = block["rows"][0]
    assert row["name"] == "pytest" and row["state"] == "fail" and "rc 1" in row["detail"]
    assert step["open"] is True  # a failed step opens


# ── planner: objective + steps markdown ─────────────────────────────────────

def test_planner_body_is_the_objective_and_steps(tmp_path: Path) -> None:
    nodes = [Node(id="planner", type="planner", state="done", start=T0, end=T0 + 3)]
    run = _run(tmp_path, nodes, [["planner"]])
    (run.run_dir / "plan.json").write_text(json.dumps({
        "objective": "Ship the demo",
        "steps": [{"id": "a", "description": "do the first thing"},
                  {"id": "b", "description": "do the second"}],
    }))
    block = _step(run_story.story_section(run), "planner")["body"][0]
    assert block["kind"] == "md"
    assert "Ship the demo" in block["text"]
    assert "**a**" in block["text"] and "do the first thing" in block["text"]


# ── fail-soft: a broken body becomes one red line ───────────────────────────

def test_a_broken_body_is_an_error_line_not_a_crash(tmp_path: Path, monkeypatch) -> None:
    nodes = [Node(id="planner", type="planner", state="done", start=T0, end=T0 + 3)]

    def boom(_run_dir):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(run_story, "_planner_body", boom)
    step = _step(run_story.story_section(_run(tmp_path, nodes, [["planner"]])), "planner")
    block = step["body"][0]
    assert block["kind"] == "lines"
    assert any("could not read" in ln["t"] for ln in block["lines"])


def test_malformed_diff_counts_do_not_blank_the_story(tmp_path: Path) -> None:
    # acp-diffs.json is not always well-formed: a non-numeric count used to
    # raise out of _files_and_note and blank the whole story. The file row
    # survives with a coerced 0/0 and every step still renders.
    nodes = [
        Node(id="implementer", type="implementer", state="done", start=T0, end=T0 + 30),
        Node(id="reviewer", type="reviewer", state="done", start=T0 + 30, end=T0 + 40),
    ]
    run = _run(tmp_path, nodes, [["implementer"], ["reviewer"]])
    (run.run_dir / "acp-diffs.json").write_text(json.dumps([
        {"path": "a.py", "added": "n/a", "removed": None},
    ]))

    story = run_story.story_section(run)
    assert [s["id"] for s in story["steps"]] == ["implementer", "reviewer"]
    block = _step(story, "implementer")["body"][0]
    assert block["kind"] == "files"
    entry = block["files"][0]
    assert entry["path"] == "a.py" and entry["added"] == 0 and entry["removed"] == 0


# ── build: the v2 Story tab is triage / (callouts) / (hero) / story ─────────

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
    events = [("node_start", "implementer", "implementer", "worker", T0 + 10, None),
              ("node_end", "implementer", "implementer", "worker", T0 + 40, "done"),
              ("node_start", "test", "verifier", "verifier", T0 + 40, None),
              ("node_end", "test", "verifier", "verifier", T0 + 45, "done"),
              ("node_start", "reviewer", "reviewer", "reviewer", T0 + 45, None),
              ("node_end", "reviewer", "reviewer", "reviewer", T0 + 80, "done")]
    for i, (kind, node, ntype, lane, ts, fin) in enumerate(events):
        payload = {"node_id": node, "node_type": ntype, "model_lane": lane}
        if fin:
            payload["finish_reason"] = fin
        con.execute("INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
                    "VALUES (?,?,?,?,?)", (f"ev-{i}", RUN, kind, json.dumps(payload), ts))
    con.commit()
    con.close()
    (run_dir / "run_profile.json").write_text(json.dumps({
        "user_goal": "Ship the demo", "success_criteria": ["it works"],
    }))
    return run_dir


def test_v2_story_tab_sections(home: Path, monkeypatch) -> None:
    _seed(home)
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = run_page.build(home, None, {"run": RUN})

    kinds = [s["type"] for s in page["sections"]]
    assert kinds[0] == "triage"
    assert kinds[-1] == "story"
    # The story replaces the Overview stopgap: no "Run inputs" / "Files changed".
    titles = [s["title"] for s in page["sections"]]
    assert "Run inputs" not in titles
    assert "Files changed" not in titles
    assert not any(s["type"] == "files" for s in page["sections"])

    story = page["sections"][-1]
    ids = [s["id"] for s in story["steps"]]
    assert "implementer" in ids and "reviewer" in ids


def test_story_tab_is_guarded_when_the_section_raises(home: Path, monkeypatch) -> None:
    # A story that raises costs the story section only: triage / hero stay, and
    # the tab closes with the guarded "Could not read this" section instead of
    # blanking the whole page.
    _seed(home)
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")

    def boom(_run):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(run_story, "story_section", boom)
    page = run_page.build(home, None, {"run": RUN})

    kinds = [s["type"] for s in page["sections"]]
    assert kinds[0] == "triage"
    assert kinds[-1] == "list"  # S.guarded's failure section
    assert page["sections"][-1]["title"] == "What happened"


def test_layers_ignore_revise_loop_edges() -> None:
    """framework-edit's verifier/reviewer → implementer ``retries`` edges are
    feedback: the implementer still comes before its verifiers and reviewer."""
    from mini_ork.ide_pages import run as run_page

    nodes = [{"name": n} for n in ("planner", "implementer", "test_verifier", "reviewer", "publisher")]
    edges = [
        {"from": "planner", "to": "implementer", "edge_type": "depends_on"},
        {"from": "implementer", "to": "test_verifier", "edge_type": "verifies"},
        {"from": "test_verifier", "to": "reviewer", "edge_type": "depends_on"},
        {"from": "reviewer", "to": "publisher", "edge_type": "depends_on"},
        {"from": "test_verifier", "to": "implementer", "edge_type": "retries"},
        {"from": "reviewer", "to": "implementer", "edge_type": "retries"},
    ]
    flat = [n for col in run_page._layers(nodes, edges) for n in col]
    assert flat.index("implementer") < flat.index("test_verifier") < flat.index("reviewer")
