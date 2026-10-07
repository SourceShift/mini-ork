"""``mini-ork board page run`` — one run's tab in the mini-ork IDE."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mini_ork.ide_pages import build_page
from mini_ork.ide_pages import run as run_page
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000000-abc123"
T0 = 1_791_000_000

WORKFLOW = """\
version: 1
task_class: demo
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md,
     gates: [scope_gate, budget_gate]}
  - {name: test, type: verifier, verifier_ref: verifiers/test.py}
  - {name: reviewer, type: reviewer, model_lane: reviewer, prompt_ref: prompts/reviewer.md}
  - {name: publisher, type: publisher, gates: [deployment_gate]}
  - {name: rollback, type: rollback}
edges:
  - {from: planner, to: implementer, edge_type: depends_on}
  - {from: implementer, to: test, edge_type: verifies}
  - {from: test, to: reviewer, edge_type: depends_on}
  - {from: reviewer, to: publisher, edge_type: depends_on}
  - {from: test, to: rollback, edge_type: escalates_to}
  - {from: reviewer, to: rollback, edge_type: escalates_to}
"""


def _iso(epoch: int) -> str:
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


def _seed(home: Path, *, status: str = "published", recipe: str = "demo-recipe") -> Path:
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True)
    kickoff.write_text("# Make the demo pass\n\nDetails.\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (RUN, recipe, status, 0.5, T0, T0 + 120, T0 + 100, "demo", str(kickoff), "latest", "tr-demo-1"))
    events = [("node_start", "implementer", "implementer", "worker", T0 + 10, None),
              ("node_end", "implementer", "implementer", "worker", T0 + 40, "done"),
              ("node_start", "test", "verifier", "verifier", T0 + 40, None),
              ("node_end", "test", "verifier", "verifier", T0 + 45, "done"),
              ("node_start", "reviewer", "reviewer", "reviewer", T0 + 45, None),
              ("node_end", "reviewer", "reviewer", "reviewer", T0 + 80, "done"),
              ("node_start", "publisher", "publisher", "publisher", T0 + 80, None),
              ("node_end", "publisher", "publisher", "publisher", T0 + 81, "done")]
    for i, (kind, node, ntype, lane, ts, fin) in enumerate(events):
        payload = {"node_id": node, "node_type": ntype, "model_lane": lane}
        if fin:
            payload["finish_reason"] = fin
        con.execute("INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
                    "VALUES (?,?,?,?,?)", (f"ev-{i}", RUN, kind, json.dumps(payload), ts))
    calls = [("planner", "glm", 0.02, T0 + 5), ("worker", "minimax", 0.07, T0 + 38),
             ("reviewer", "glm", 0.11, T0 + 79), ("gradient-extract", "glm", 0.03, T0 + 110)]
    for actor, model, cost, ts in calls:
        con.execute("INSERT INTO llm_calls (provider, model_id, tier, feature_name, actor, run_id, "
                    "cost_usd, status, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("gateway", model, "default", f"mini-ork:{actor}", actor, RUN, cost, "success", _iso(ts)))
    con.execute("INSERT INTO gradient_records (gradient_id, target, signal, suggested_change, evidence, "
                "confidence, created_at, task_class) VALUES (?,?,?,?,?,?,?,?)",
                ("gr-demo1", "workflow.node.implementer", "the implementer skipped the test",
                 "run the test first", "tr-x", 0.7, T0 + 115, "demo"))
    # Bridge for `_learnings_tab`'s run-scoped gradient join: `task_runs.id` is
    # TEXT (e.g. ``run-<ts>-<pid>``) and live `execution_traces.run_id` stores
    # the same text — mirror that shape here so the join exercised by
    # `test_agents_learnings_and_artifacts` reflects production.
    con.execute("INSERT INTO execution_traces (trace_id, run_id, status, task_class) "
                "VALUES (?, ?, ?, ?)", ("tr-x", RUN, "success", "demo"))
    con.commit()
    con.close()
    (run_dir / "plan.json").write_text('{"objective": "x"}')
    (run_dir / "context-pack.json").write_text(json.dumps({
        "prior_similar_runs": [{"cite": "execution_traces/tr-1"}], "known_failure_modes": [],
        "tokens_estimated": 900, "budget_tokens": 64000}))
    (run_dir / "verifier_test.json").write_text(
        '[test] running: pytest\n{"verifier": "test", "pass": true, "post_rc": 0, '
        '"error_summary": "suite green", "evidence_path": null}\n')
    (run_dir / "agent-implementer.live.jsonl").write_text(
        json.dumps({"seq": 0, "line": "Edit calc.py"}) + "\n"
        + json.dumps({"seq": 1, "line": "tests PASS"}) + "\n")
    (run_dir / "review-reviewer.json").write_text('```json\n{"verdict": "pass"}\n```\n')
    (run_dir / "verdict.json").write_text('{"verdict": "pass"}')
    return run_dir


def _section(page: dict, kind: str, title: str | None = None) -> dict:
    for s in page["sections"]:
        if s["type"] == kind and (title is None or s["title"] == title):
            return s
    raise AssertionError(f"no {kind} section {title!r} in {[s['title'] for s in page['sections']]}")


def test_dag_layers_the_workflow_and_attributes_cost(home: Path) -> None:
    _seed(home)
    page = build_page(home, "run", None, {"run": RUN})
    assert page["ok"] is True and page["tab"] == "dag" and page["title"] == RUN
    assert [t["key"] for t in page["tabs"]] == ["dag", "overview", "agents", "learnings", "artifacts"]
    assert [c["t"] for c in page["chips"]][:2] == ["published · verified", "5/6 nodes"]
    dag = _section(page, "dag")
    cols = [[n["id"] for n in c] for c in dag["cols"]]
    assert cols == [["planner"], ["implementer"], ["test"], ["reviewer"], ["publisher"], ["rollback"]]
    nodes = {n["id"]: n for c in dag["cols"] for n in c}
    # The planner never emits lifecycle events, but its call proves it ran.
    assert nodes["planner"]["state"] == "done" and nodes["planner"]["lane"] == "glm"
    assert nodes["implementer"]["lane"] == "minimax" and nodes["implementer"]["cost"] == "$0.07"
    assert nodes["test"]["lane"] == "shell" and nodes["test"]["cost"] == ""
    assert nodes["rollback"]["state"] == "skipped"
    assert nodes["implementer"]["do"] == {"set": {"node": "implementer"}}
    # Nothing running or failed: the last finished node is inspected.
    assert nodes["publisher"]["sel"] is True
    coalition = _section(page, "pills")
    assert coalition["title"].startswith("Coalition")
    json.dumps(page)


def test_inspector_shows_the_selected_nodes_real_output(home: Path) -> None:
    _seed(home)
    page = build_page(home, "run", "dag", {"run": RUN, "node": "implementer"})
    insp = _section(page, "inspector")
    assert insp["label"] == "implementer" and insp["state"] == "done"
    assert [ln["t"] for ln in insp["lines"]] == ["Edit calc.py", "tests PASS"]
    assert insp["lines"][1]["c"] == "green"
    items = {i["k"]: i["v"] for i in insp["items"]}
    assert items["Gates"] == "scope_gate, budget_gate"
    assert items["Cost"].startswith("$0.07")
    assert items["Wall time"] == "30s"
    assert items["Prompt"] == "demo-recipe/prompts/implementer.md"
    assert insp["steer"] is None
    assert page["args"] == {"run": RUN, "node": "implementer"}


def test_overview_reads_verifier_files_with_log_lines_before_the_json(home: Path) -> None:
    _seed(home)
    page = build_page(home, "run", "overview", {"run": RUN})
    evidence = _section(page, "list", "Why? — evidence")
    assert evidence["items"][0]["t"] == "test → PASS rc=0"
    inputs = _section(page, "list", "Run inputs")
    names = [i["t"] for i in inputs["items"]]
    assert names[:2] == ["demo.md", "plan.json"] and "context-pack.json" in names
    corr = {i["k"]: i["v"] for i in _section(page, "kv", "Correlation")["items"]}
    assert corr["trace_id"] == "tr-demo-1" and corr["events"] == "8" and corr["LLM calls"] == "4"
    events = _section(page, "code", "Recent events")["lines"]
    assert events[-1]["t"].split()[2:4] == ["node_end", "publisher"]


def test_agents_learnings_and_artifacts(home: Path) -> None:
    run_dir = _seed(home)
    agents = build_page(home, "run", "agents", {"run": RUN})
    roster = _section(agents, "table", "Agent roster")
    rows = {r["cells"][0]["t"]: [c["t"] for c in r["cells"]] for r in roster["rows"]}
    assert rows["reviewer"][1:4] == ["glm", "done", "pass"]
    assert rows["test"][3] == "pass" and rows["rollback"][2] == "not run"
    assert roster["rows"][0]["do"] == {"page": "run", "tab": "dag", "args": {"run": RUN, "node": "planner"}}

    learn = build_page(home, "run", "learnings", {"run": RUN})
    produced = _section(learn, "list", "Produced by the run")
    # Title is the gradient's signal (not its gradient_id); the trace id
    # `tr-x` doesn't parse to a node name, so the sub falls back to the raw id.
    assert produced["items"][0]["t"] == "the implementer skipped the test"
    assert produced["items"][0]["m"] == "✦"
    assert "fix: run the test first" in produced["items"][0]["sub"]
    assert "tr-x" in produced["items"][0]["sub"]
    assert "confidence 0.70" in produced["items"][0]["sub"]
    available = {i["t"]: i for i in _section(learn, "list", "Available to the run")["items"]}
    assert available["Prior same-class runs"]["m"] == "✓"
    # New summary drops the cite — only the count remains.
    assert available["Prior same-class runs"]["sub"] == "1 item"
    assert available["Learned failure modes"]["sub"] == "none given"

    arts = build_page(home, "run", "artifacts", {"run": RUN})
    table = _section(arts, "table", "Artifacts")
    by_file = {r["cells"][0]["t"]: r for r in table["rows"]}
    assert by_file["plan.json"]["do"] == {"path": str(run_dir / "plan.json")}
    assert by_file["agent-implementer.live.jsonl"]["cells"][3]["t"] == "implementer"


def test_actions_follow_the_run_state(home: Path) -> None:
    _seed(home, status="executing")
    page = build_page(home, "run", None, {"run": RUN})
    labels = [a["label"] for a in page["actions"]]
    assert labels[0] == "Stop" and page["actions"][0]["do"]["cli"] == ["board", "stop", RUN]
    assert page["actions"][0]["do"]["confirm"]
    kill = next(a for a in page["actions"] if a["label"] == "Kill")
    assert kill["do"]["cli"] == ["board", "kill", RUN]
    assert kill["do"]["confirm"].startswith("Kill")
    assert page["chips"][0] == {"t": "running", "c": "blue"}
    assert "Certify this change" not in labels


def test_a_run_without_a_known_recipe_lays_nodes_out_by_time(home: Path) -> None:
    _seed(home, recipe="gone-recipe")
    page = build_page(home, "run", None, {"run": RUN})
    cols = [[n["id"] for n in c] for c in _section(page, "dag")["cols"]]
    assert cols == [["implementer"], ["test"], ["reviewer"], ["publisher"]]


def test_a_broken_source_costs_one_section(home: Path, monkeypatch) -> None:
    _seed(home)

    def boom(*_a, **_k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(run_page, "_node_output", boom)
    page = build_page(home, "run", "dag", {"run": RUN})
    assert _section(page, "dag")["cols"]
    broken = _section(page, "list", "Inspector")
    assert broken["items"][0]["m"] == "✗" and "disk gone" in broken["items"][0]["sub"]


def test_missing_or_unknown_run(home: Path) -> None:
    assert build_page(home, "run", None, {})["ok"] is False
    out = build_page(home, "run", None, {"run": "run-nope"})
    assert out["ok"] is False and "no run run-nope" in out["error"]
