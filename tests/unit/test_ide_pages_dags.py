"""The dags page contract: one batched thumbnail of the workflow DAG per run."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mini_ork.ide_pages import build_page
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN_A = "run-dag-aaaa"
RUN_B = "run-dag-bbbb"
RUN_C = "run-dag-cccc"
RUN_NO_RECIPE = "run-dag-no-recipe"
RUN_NO_EVENTS = "run-dag-no-events"
T0 = 1_791_000_000

WORKFLOW = """\
version: 1
task_class: demo
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md}
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

# Override the engine's agents.yaml with a known lane map so the dags lane
# assertions are deterministic. Keys mirror the WORKFLOW's ``model_lane``
# fields: planner / worker / reviewer.
LANES = """\
lanes:
  planner: glm
  worker: minimax
  reviewer: opus
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    (h / "config").mkdir()
    (h / "config" / "agents.yaml").write_text(LANES)
    recipe = h / "recipes" / "demo-recipe"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(WORKFLOW)
    (recipe / "task_class.yaml").write_text("name: demo\ndescription: a demo recipe\n")
    # A second recipe that does NOT have workflow.yaml on disk — exercises the
    # "recipe not found" path without breaking the rest of the batch.
    (h / "recipes" / "ghost-recipe").mkdir()
    return h


def _seed_run(home: Path, run_id: str, recipe: str,
              events: list[tuple[str, str, str, str, int, str | None]]) -> None:
    """Insert a ``task_runs`` row plus an ``run_events`` batch for one run.

    Each event tuple is ``(event_type, node_id, node_type, model_lane,
    ts_epoch, finish_reason_or_None)`` — mirrors the kwargs used by the run-page
    fixture so the dags page sees byte-equivalent ``payload_json`` shapes.
    """
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, "
        "ended_at, task_class, kickoff_path, workflow_version, trace_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, recipe, "published", 0.5, T0, T0 + 120, T0 + 100,
         "demo", "kickoffs/demo.md", "latest", f"tr-{run_id}"))
    for i, (kind, node, ntype, lane, ts, fin) in enumerate(events):
        payload = {"node_id": node, "node_type": ntype, "model_lane": lane}
        if fin:
            payload["finish_reason"] = fin
        con.execute(
            "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (f"ev-{run_id}-{i}", run_id, kind, json.dumps(payload), ts))
    con.commit()
    con.close()


def _events_for_demo() -> list[tuple[str, str, str, str, int, str | None]]:
    """The minimal lifecycle trace needed to mark 4/6 nodes ``done``."""
    return [
        ("node_start", "implementer", "implementer", "worker", T0 + 10, None),
        ("node_end", "implementer", "implementer", "worker", T0 + 40, "done"),
        ("node_start", "test", "verifier", "verifier", T0 + 40, None),
        ("node_end", "test", "verifier", "verifier", T0 + 45, "done"),
        ("node_start", "reviewer", "reviewer", "reviewer", T0 + 45, None),
        ("node_end", "reviewer", "reviewer", "reviewer", T0 + 80, "done"),
        ("node_start", "publisher", "publisher", "publisher", T0 + 80, None),
        ("node_end", "publisher", "publisher", "publisher", T0 + 81, "done"),
    ]


def test_dags_page_returns_one_dag_per_run_with_full_edges(home: Path) -> None:
    """Every edge in the workflow's ``edges`` list surfaces verbatim —
    including the ``escalates_to`` type — and ``nodes[].lane`` is the
    provider lane from ``agents.yaml``, not the role key from
    ``model_lane``."""
    _seed_run(home, RUN_A, "demo-recipe", _events_for_demo())
    _seed_run(home, RUN_B, "demo-recipe", _events_for_demo())

    page = build_page(home, "dags", None, {"ids": f"{RUN_A},{RUN_B}"})

    assert page["ok"] is True and page["label"] == "DAGs"
    assert set(page["dags"]) == {RUN_A, RUN_B}

    for run_id in (RUN_A, RUN_B):
        dag = page["dags"][run_id]
        assert dag["recipe"] == "demo-recipe"
        node_ids = {n["id"] for n in dag["nodes"]}
        assert node_ids == {"planner", "implementer", "test", "reviewer", "publisher", "rollback"}
        # Provider lanes, not role keys (model_lane names from WORKFLOW).
        lanes = {n["id"]: n["lane"] for n in dag["nodes"]}
        assert lanes["planner"] == "glm"           # role key "planner" → "glm"
        assert lanes["implementer"] == "minimax"   # role key "worker"    → "minimax"
        assert lanes["reviewer"] == "opus"         # role key "reviewer"  → "opus"
        # Nodes without ``model_lane`` (test/publisher/rollback) surface as
        # ``None`` — fingerprint's own behaviour. The dags page must not
        # silently coerce them to "shell".
        assert lanes["test"] is None
        assert lanes["publisher"] is None
        assert lanes["rollback"] is None

        # All six edges with their edge_type verbatim, including escalates_to.
        edges = sorted((e["from"], e["to"], e["edge_type"]) for e in dag["edges"])
        assert edges == [
            ("implementer", "test", "verifies"),
            ("planner", "implementer", "depends_on"),
            ("reviewer", "publisher", "depends_on"),
            ("reviewer", "rollback", "escalates_to"),
            ("test", "reviewer", "depends_on"),
            ("test", "rollback", "escalates_to"),
        ]

        # Statuses derived from the seeded events: 4 nodes ran (done),
        # planner + rollback never emitted lifecycle events.
        states = {n["id"]: n["state"] for n in dag["nodes"]}
        assert states["implementer"] == "done"
        assert states["test"] == "done"
        assert states["reviewer"] == "done"
        assert states["publisher"] == "done"
        assert states["planner"] == "never_seen"
        assert states["rollback"] == "never_seen"

    # Per-run isolation: no errors, the rest of the batch is intact.
    assert page["errors"] == {}

    # The whole payload must be JSON-serialisable for the board's renderer.
    json.dumps(page)


def test_a_run_with_an_unknown_recipe_records_in_errors_not_in_dags(home: Path) -> None:
    """A recipe whose directory no longer resolves MUST land in ``errors``
    and MUST NOT abort the rest of the batch."""
    _seed_run(home, RUN_NO_RECIPE, "ghost-recipe", _events_for_demo())
    _seed_run(home, RUN_C, "demo-recipe", _events_for_demo())

    page = build_page(home, "dags", None, {"ids": f"{RUN_NO_RECIPE},{RUN_C}"})

    assert page["ok"] is True
    assert RUN_NO_RECIPE in page["errors"]
    assert RUN_NO_RECIPE not in page["dags"]
    # The good run is still rendered.
    assert page["dags"][RUN_C]["recipe"] == "demo-recipe"
    assert {n["id"] for n in page["dags"][RUN_C]["nodes"]} == {
        "planner", "implementer", "test", "reviewer", "publisher", "rollback"
    }


def test_a_run_with_no_run_events_yields_all_never_seen(home: Path) -> None:
    """A run with zero ``run_events`` rows must yield ``never_seen`` per node,
    not an exception."""
    _seed_run(home, RUN_NO_EVENTS, "demo-recipe", [])  # no events seeded

    page = build_page(home, "dags", None, {"ids": RUN_NO_EVENTS})

    assert page["ok"] is True
    dag = page["dags"][RUN_NO_EVENTS]
    assert dag["recipe"] == "demo-recipe"
    states = [n["state"] for n in dag["nodes"]]
    assert states == ["never_seen"] * len(dag["nodes"])
    # Edges still come from the recipe (not the events) — verify a couple.
    edge_types = {e["edge_type"] for e in dag["edges"]}
    assert edge_types == {"depends_on", "verifies", "escalates_to"}
    json.dumps(page)


def test_ids_absent_falls_back_to_recent_runs(home: Path) -> None:
    """No ``ids`` arg → page returns the most recent runs (limit 50) without
    crashing."""
    _seed_run(home, RUN_A, "demo-recipe", _events_for_demo())

    page = build_page(home, "dags", None, {})

    assert page["ok"] is True
    assert RUN_A in page["dags"]
    json.dumps(page)


def test_empty_state_db_returns_an_empty_payload(home: Path) -> None:
    """No runs at all → ``{ok, label, dags: {}, errors: {}}`` — never a 500."""
    page = build_page(home, "dags", None, {"ids": "run-does-not-exist"})
    assert page["ok"] is True and page["dags"] == {} and page["errors"] == {}


def test_each_node_carries_its_type_and_timing(home: Path) -> None:
    """The board shows each node's elapsed time: ``started_at`` is the
    node_start epoch, ``duration_ms`` the node_end payload's (``None`` until
    seen), and ``type`` the workflow node type."""
    events = _events_for_demo()
    _seed_run(home, RUN_A, "demo-recipe", events)
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "UPDATE run_events SET payload_json = json_set(payload_json, '$.duration_ms', 30000) "
        "WHERE run_id = ? AND event_type = 'node_end' AND payload_json LIKE '%\"implementer\"%'",
        (RUN_A,))
    con.commit()
    con.close()

    nodes = {n["id"]: n for n in build_page(home, "dags", None, {"ids": RUN_A})["dags"][RUN_A]["nodes"]}
    assert nodes["implementer"]["type"] == "implementer"
    assert nodes["implementer"]["started_at"] == T0 + 10
    assert nodes["implementer"]["duration_ms"] == 30000
    assert nodes["planner"]["started_at"] is None and nodes["planner"]["duration_ms"] is None
