"""Per-lane health in the status-bar header, and the run page's steer composer.

``header._lanes`` feeds the status bar's per-lane mini-bars (today's calls,
failures and $ against the daily cap) from ``llm_calls``; a run page's Story tab
grows a ``composer`` that runs ``mini-ork board steer <run> --text <typed>`` for
a live run.

The run-page fixture below is a deliberate copy of the one in
``tests/unit/test_ide_pages_run.py`` (the pattern the kickoff names): the two
files pin different behaviour and must not share private helpers.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mini_ork.ide_pages import build_page
from mini_ork.ide_pages import header as header_mod
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


def _iso(epoch: float) -> str:
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


# ── _lanes ──────────────────────────────────────────────────────────────────


def _seed_call(con: sqlite3.Connection, *, actor: str | None, provider: str, status: str,
               cost: float, ts: float, error: str | None = None) -> None:
    """One ``llm_calls`` row; ``actor`` is the lane label unless it is NULL."""
    con.execute(
        "INSERT INTO llm_calls (provider, model_id, tier, feature_name, actor, cost_usd, "
        "status, error_message, ts) VALUES (?,?,?,?,?,?,?,?,?)",
        (provider, "m", "default", "mini-ork:x", actor, cost, status, error, _iso(ts)))


def test_lanes_rolls_up_by_actor_then_provider(home: Path) -> None:
    """Two lanes: a named one (``actor``) with a failure whose message rides
    ``last_error`` cut to 120 chars, and the ``provider`` fallback. A row older
    than 24 h is excluded — its $9.99 never touches the lane's spend."""
    con = sqlite3.connect(home / "state.db")
    now = time.time()
    long_error = "x" * 200
    _seed_call(con, actor="codex", provider="openai", status="success", cost=0.10, ts=now - 3600)
    _seed_call(con, actor="codex", provider="openai", status="failed", cost=0.05, ts=now - 1800,
               error=long_error)
    _seed_call(con, actor=None, provider="openrouter", status="success", cost=0.30, ts=now - 600)
    _seed_call(con, actor="codex", provider="openai", status="failed", cost=9.99, ts=now - 90_000,
               error="ancient")
    con.commit()
    con.close()

    lanes = header_mod._lanes(home / "state.db")
    assert [lane["lane"] for lane in lanes] == ["codex", "openrouter"]  # calls desc
    assert lanes[0] == {"lane": "codex", "calls": 2, "failed": 1, "usd": 0.15,
                        "last_error": long_error[:120]}
    assert lanes[1] == {"lane": "openrouter", "calls": 1, "failed": 0, "usd": 0.3,
                        "last_error": ""}


def test_lanes_keeps_the_newest_failing_message_per_lane(home: Path) -> None:
    """``last_error`` is the newest failing row's message, not the newest row's
    (a later success must not blank it) and not the oldest failure."""
    con = sqlite3.connect(home / "state.db")
    now = time.time()
    _seed_call(con, actor="glm", provider="zhipu", status="failed", cost=0.01, ts=now - 3000,
               error="old failure")
    _seed_call(con, actor="glm", provider="zhipu", status="failed", cost=0.01, ts=now - 1200,
               error="recent failure")
    _seed_call(con, actor="glm", provider="zhipu", status="success", cost=0.02, ts=now - 60)
    con.commit()
    con.close()

    lanes = header_mod._lanes(home / "state.db")
    assert lanes[0]["last_error"] == "recent failure"
    assert lanes[0]["failed"] == 2


def test_lanes_without_the_llm_calls_table_is_empty(tmp_path: Path) -> None:
    """A DB that predates the migrator (no ``llm_calls``) degrades to ``[]``."""
    db = tmp_path / "other.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE unrelated (x INTEGER)")
    con.commit()
    con.close()
    assert header_mod._lanes(db) == []


def test_lanes_without_the_actor_column_is_empty(tmp_path: Path) -> None:
    """An ``llm_calls`` table that predates the columns the roll-up reads (a DB
    behind the migrator) also degrades to ``[]`` — not an exception."""
    db = tmp_path / "partial.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, provider TEXT, "
                "status TEXT, cost_usd REAL, ts TEXT)")
    con.commit()
    con.close()
    assert header_mod._lanes(db) == []


def test_lanes_for_a_missing_db_is_empty(tmp_path: Path) -> None:
    assert header_mod._lanes(tmp_path / "nope.db") == []


def test_header_carries_lanes(home: Path) -> None:
    con = sqlite3.connect(home / "state.db")
    _seed_call(con, actor="codex", provider="openai", status="failed", cost=0.02,
               ts=time.time() - 60, error="rate limited")
    con.commit()
    con.close()
    payload = header_mod.header(home, {})
    assert payload["lanes"] == [{"lane": "codex", "calls": 1, "failed": 1, "usd": 0.02,
                                 "last_error": "rate limited"}]


# ── the run page's steer composer ───────────────────────────────────────────


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
        (RUN, recipe, status, 0.5, T0, T0 + 120, T0 + 100, "demo", str(kickoff), "latest",
         "tr-demo-1"))
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
                    ("gateway", model, "default", f"mini-ork:{actor}", actor, RUN, cost, "success",
                     _iso(ts)))
    con.execute("INSERT INTO execution_traces (trace_id, run_id, status, task_class) "
                "VALUES (?, ?, ?, ?)", ("tr-x", RUN, "success", "demo"))
    con.commit()
    con.close()
    (run_dir / "plan.json").write_text('{"objective": "x"}')
    (run_dir / "verdict.json").write_text('{"verdict": "pass"}')
    return run_dir


def test_a_live_run_story_ends_with_the_steer_composer(home: Path, monkeypatch) -> None:
    _seed(home, status="executing")
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = build_page(home, "run", None, {"run": RUN})
    assert page["tab"] == "story"
    last = page["sections"][-1]
    assert last["type"] == "composer"
    assert last["title"] == "Steer this run"
    assert last["placeholder"].startswith("Tell the running agent")
    assert last["cli"] == ["board", "steer", RUN, "--text"]


def test_a_finished_run_story_has_no_composer(home: Path, monkeypatch) -> None:
    _seed(home, status="published")
    monkeypatch.setenv("MINI_ORK_IDE_SPEC", "2")
    page = build_page(home, "run", None, {"run": RUN})
    assert page["tab"] == "story"
    assert all(s["type"] != "composer" for s in page["sections"])
