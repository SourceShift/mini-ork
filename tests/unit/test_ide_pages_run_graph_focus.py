"""``graph.focus`` and node ``reason`` — the kickoff run-page/rp-e6-graph-focus.

The DAG strip used to open on the node that ran *last*, which on a failed run is
often not the failed node (``hr-smoke-d-191036``: the reviewer failed while the
eval and publisher ran after it). These fixtures mirror
``tests/unit/test_ide_pages_run.py`` — a real ``state.db``, a real
``workflow.yaml``, real ``run_events`` — and assert through the built page, not
through a monkeypatched helper.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from mini_ork.ide_pages import build_page
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000000-focus"
T0 = 1_791_000_000

WORKFLOW = """\
version: 1
task_class: demo
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md}
  - {name: test, type: verifier, verifier_ref: verifiers/test.py}
  - {name: reviewer, type: reviewer, model_lane: reviewer, prompt_ref: prompts/reviewer.md}
  - {name: publisher, type: publisher}
edges:
  - {from: planner, to: implementer, edge_type: depends_on}
  - {from: implementer, to: test, edge_type: verifies}
  - {from: test, to: reviewer, edge_type: depends_on}
  - {from: reviewer, to: publisher, edge_type: depends_on}
"""


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


def _seed(home: Path, *, status: str, events: list[tuple[str, str, str]],
          recipe: str = "demo-recipe") -> Path:
    """One run: its ``task_runs`` row plus ``(event_type, node_id, finish_reason)``.

    ``node_end`` with a ``finish_reason`` other than ``"done"``/``""`` is a
    failed step (``fleet._steps``); a ``node_start`` with no ``node_end`` is a
    running one. Same shape ``test_ide_pages_run._seed`` builds.
    """
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True, exist_ok=True)
    kickoff.write_text("# Make the demo pass\n\nDetails.\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (RUN, recipe, status, 0.0, T0, T0 + 120, T0 + 120, "demo", str(kickoff),
         "latest", "tr-focus"))
    for i, (kind, node, fin) in enumerate(events):
        payload = {"node_id": node, "node_type": "worker", "model_lane": "worker"}
        if fin:
            payload["finish_reason"] = fin
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?,?,?,?,?)", (f"ev-{i}", RUN, kind, json.dumps(payload), T0 + 10 + i))
    con.commit()
    con.close()
    return run_dir


def _park(home: Path, run_dir: Path, node_id: str) -> None:
    """Mark the run parked for human review of ``node_id`` (the real signals)."""
    from mini_ork.gates import node_review, oversight_inbox

    con = sqlite3.connect(home / "state.db")
    con.execute("UPDATE task_runs SET status = 'reviewing' WHERE id = ?", (RUN,))
    con.commit()
    con.close()
    (run_dir / ".review-pause").write_text("", encoding="utf-8")
    oversight_inbox.enqueue(f"node:{RUN}:{node_id}:{int(time.time())}",
                            feature=node_review.FEATURE,
                            context={"node_id": node_id},
                            db_path=str(home / "state.db"))


def _graph(home: Path) -> dict:
    page = build_page(home, "run", "graph", {"run": RUN})
    assert page["ok"] is True, page
    assert "graph" in page
    return page["graph"]


def _nodes(graph: dict) -> dict[str, dict]:
    return {n["id"]: n for col in graph["cols"] for n in col}


def test_focus_is_the_failed_node_even_when_a_later_node_ran(home: Path) -> None:
    """The kickoff's own example: ``hr-smoke-d-191036`` failed on ``reviewer``
    while ``eval`` and ``publisher`` ran after it — the strip must open on the
    failed node, not on the node that happened to run last."""
    run_dir = _seed(home, status="failed", events=[
        ("node_start", "planner", ""), ("node_end", "planner", "done"),
        ("node_start", "implementer", ""), ("node_end", "implementer", "done"),
        ("node_start", "test", ""), ("node_end", "test", "done"),
        ("node_start", "reviewer", ""), ("node_end", "reviewer", "verdict_fail"),
        ("node_start", "publisher", ""), ("node_end", "publisher", "done"),
    ])
    (run_dir / "verifier_reviewer.json").write_text(json.dumps(
        {"pass": False, "error": "judge panel returned needs_revision"}))
    graph = _graph(home)
    assert graph["focus"] == "reviewer"
    # ...and the failed node says why, in one line.
    reason = _nodes(graph)["reviewer"].get("reason")
    assert isinstance(reason, str) and reason, "the focused failed node needs a reason"
    assert len(reason) <= 80 and "\n" not in reason


def test_focus_is_the_pending_review_node_on_a_parked_run(home: Path) -> None:
    run_dir = _seed(home, status="executing", events=[
        ("node_start", "planner", ""), ("node_end", "planner", "done"),
        ("node_start", "implementer", ""), ("node_end", "implementer", "done"),
    ])
    _park(home, run_dir, "reviewer")
    graph = _graph(home)
    assert graph["focus"] == "reviewer"


def test_focus_falls_back_to_the_running_node(home: Path) -> None:
    _seed(home, status="executing", events=[
        ("node_start", "planner", ""), ("node_end", "planner", "done"),
        ("node_start", "implementer", ""),
    ])
    graph = _graph(home)
    assert graph["focus"] == "implementer"


def test_reason_is_one_capped_line_from_the_verifier_json(home: Path) -> None:
    run_dir = _seed(home, status="failed", events=[
        ("node_start", "planner", ""), ("node_end", "planner", "done"),
        ("node_start", "reviewer", ""), ("node_end", "reviewer", "verdict_fail"),
    ])
    (run_dir / "verifier_planner.json").write_text(json.dumps({"pass": True}))
    (run_dir / "verifier_reviewer.json").write_text(json.dumps({
        "pass": False,
        "error": ("judge panel returned needs_revision: the diff " + "z" * 200
                  + "\nsecond line"),
    }))
    graph = _graph(home)
    nodes = _nodes(graph)
    reason = nodes["reviewer"].get("reason")
    assert isinstance(reason, str) and reason
    assert len(reason) <= 80 and "\n" not in reason
    # A node that passed is not failed and not blocked: it gets no reason key.
    assert "reason" not in nodes["planner"]
