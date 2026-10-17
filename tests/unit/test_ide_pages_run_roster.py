"""The run page's one roster: Agents only, and fallback lanes flagged.

Fixtures build a ``Run`` directly and attribute its ``llm_calls`` with
``_attribute_calls`` — the same path ``_load`` takes — so everything the
roster shows comes from the run's own calls. Hermetic: ``tmp_path`` only, no
DB, no network, no run-local state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mini_ork.ide_pages import run as run_page

BASE = 1_790_000_000
RUN_ID = "run-roster-unit"
BASE_NOTE = "Where the money went inside the run. Click a row to inspect the node."
MARKER = "⇄"


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _call(actor: str, model: str, ts: int, status: str = "success") -> dict:
    return {"provider": "loop", "model_id": model, "actor": actor, "ts": _iso(ts),
            "status": status, "cost_usd": 0.01, "finish_reason": "stop"}


def _node(node_id: str, lane: str, start: int = BASE, end: int = BASE + 600) -> run_page.Node:
    return run_page.Node(id=node_id, type="implementer", role_lane=lane,
                         state="done", start=start, end=end)


def _run(tmp_path: Path, nodes: list[run_page.Node], calls: list[dict]) -> run_page.Run:
    by_id = {n.id: n for n in nodes}
    run_page._attribute_calls(by_id, calls, BASE + 10_000)
    run_dir = tmp_path / "runs" / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_page.Run(id=RUN_ID, home=tmp_path, run_dir=run_dir, row={}, card={},
                        nodes=list(by_id.values()), cols=[], calls=list(calls),
                        recipe_dir=None, workspace=None)


def _row(section: dict, node_id: str) -> dict:
    for row in section["rows"]:
        if row.get("cells") and row["cells"][0].get("t") == node_id:
            return row
    raise AssertionError(f"no roster row for {node_id!r}")


def _cell(section: dict, node_id: str, column: str) -> str:
    idx = section["head"].index(column)
    return _row(section, node_id)["cells"][idx].get("t")


def _markers(section: dict, node_id: str) -> list[str]:
    return [c.get("t") for c in _row(section, node_id)["cells"] if c.get("t") == MARKER]


def test_dag_tab_has_no_roster_and_agents_tab_has_exactly_one(tmp_path):
    run = _run(tmp_path,
               nodes=[_node("a1", "worker"), _node("a2", "planner")],
               calls=[_call("worker", "minimax", BASE + 10),
                      _call("planner", "glm", BASE + 20)])

    titles = [s.get("title") for s in run_page._dag_tab(run, None)]
    assert "Agent roster" not in titles and "Roster" not in titles, titles
    # The DAG keeps its own sections; only the roster entry goes.
    assert "DAG" in titles and "Run totals" in titles, titles
    assert len(titles) == 4, titles

    v2_titles = [s.get("title") for s in run_page._dag_tab(
        run, None, select=True, outcome={}, callout_actions=[])]
    assert "Agent roster" not in v2_titles, v2_titles

    agents = [s for s in run_page._agents_tab(run) if s.get("title") == "Agent roster"]
    assert len(agents) == 1
    # ...and it is still the single builder's payload.
    assert agents[0] == run_page._roster_section(run, tokens=True)


def test_two_models_render_first_to_last_and_a_fallback_list(tmp_path):
    run = _run(tmp_path,
               nodes=[_node("a1", "worker"), _node("a2", "planner")],
               calls=[_call("worker", "minimax", BASE + 10),
                      _call("worker", "minimax", BASE + 50),
                      _call("worker", "glm", BASE + 100),
                      _call("planner", "glm", BASE + 20)])

    for section in (run_page._roster_section(run),
                    run_page._roster_section(run, tokens=True)):
        assert _cell(section, "a1", "family") == "minimax → glm"
        assert _row(section, "a1").get("fallback") == ["minimax", "glm"]
        assert _markers(section, "a1") == [MARKER]
        assert section["note"].startswith(BASE_NOTE)
        assert "(a1: minimax → glm)" in section["note"]
        assert "1 node fell back" in section["note"]


def test_two_fallbacks_name_each_sequence_in_its_own_time_order(tmp_path):
    run = _run(tmp_path,
               nodes=[_node("a1", "worker"), _node("b9", "reviewer")],
               calls=[_call("worker", "minimax", BASE + 10),
                      _call("worker", "glm", BASE + 100),
                      _call("reviewer", "glm", BASE + 30),
                      _call("reviewer", "minimax", BASE + 200)])

    section = run_page._roster_section(run, tokens=True)
    assert "2 nodes fell back" in section["note"]
    assert "(a1: minimax → glm)" in section["note"]
    assert "(b9: glm → minimax)" in section["note"]
    assert _cell(section, "b9", "family") == "glm → minimax"
    assert _row(section, "b9").get("fallback") == ["glm", "minimax"]


def test_zero_and_one_model_rows_are_unchanged(tmp_path):
    run = _run(tmp_path,
               nodes=[_node("a1", "worker"), _node("a2", "planner"), _node("a3", "shell")],
               calls=[_call("worker", "minimax", BASE + 10),
                      _call("worker", "glm", BASE + 100),
                      _call("planner", "glm", BASE + 20)])

    section = run_page._roster_section(run, tokens=True)
    assert _cell(section, "a2", "family") == "glm"
    assert _cell(section, "a2", "status") == "done"
    assert _cell(section, "a2", "verdict") == "ok"
    assert _cell(section, "a2", "cost") == "$0.01"
    assert "fallback" not in _row(section, "a2")
    assert _markers(section, "a2") == []

    # Zero calls: the cell stays empty — a sibling's models never leak in.
    assert _cell(section, "a3", "family") == ""
    assert "fallback" not in _row(section, "a3")
    assert _markers(section, "a3") == []
    assert "(a2" not in section["note"] and "(a3" not in section["note"]


def test_failed_call_before_success_is_named_in_the_note(tmp_path):
    run = _run(tmp_path,
               nodes=[_node("a1", "worker")],
               calls=[_call("worker", "minimax", BASE + 10, status="failed"),
                      _call("worker", "glm", BASE + 100, status="success")])

    section = run_page._roster_section(run, tokens=True)
    assert "failed call, then glm" in section["note"]
    assert _row(section, "a1").get("fallback") == ["minimax", "glm"]
    assert _cell(section, "a1", "family") == "minimax → glm"


def test_failed_call_after_success_is_not_a_failed_then_note(tmp_path):
    run = _run(tmp_path,
               nodes=[_node("a1", "worker")],
               calls=[_call("worker", "minimax", BASE + 10, status="success"),
                      _call("worker", "glm", BASE + 100, status="failed")])

    section = run_page._roster_section(run, tokens=True)
    assert "failed call" not in section["note"]
    assert "fell back" in section["note"]
    assert "(a1: minimax → glm)" in section["note"]


def test_no_fallbacks_leaves_the_note_byte_identical(tmp_path):
    run = _run(tmp_path,
               nodes=[_node("a1", "worker"), _node("a2", "planner")],
               calls=[_call("worker", "glm", BASE + 10),
                      _call("planner", "minimax", BASE + 20)])

    for section in (run_page._roster_section(run),
                    run_page._roster_section(run, tokens=True)):
        assert section["note"] == BASE_NOTE
