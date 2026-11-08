"""Unit tests: decide()'s code_region reaches execution_traces.code_region.

The routing brain already derives a region for implementer nodes from the run's
declared scope; this pins the propagation chain that used to drop it:

  policy_route_lane -> _record_route -> last_route_provenance()
                    -> _make_trace_fn -> trace_store.trace_write

Two properties matter and are easy to "helpfully" break: a NULL decision must
stay NULL (no fallback to infer_trace_code_region / the run-dir path / a scope
guess), and a later NULL re-write must not blank a region already stored
(coalesce).

Hermetic: temp SQLite DBs and run dirs only, stubbed ``decide`` where the brain
itself is not under test, no network.
"""
from __future__ import annotations

import json
import os
import sqlite3

from mini_ork import trace_store
from mini_ork.cli.execute import _make_trace_fn
from mini_ork.dispatch.routing import (
    REGION_NODE_TYPES,
    last_route_provenance,
    policy_route_lane,
)

SCOPE_REGION = "mini_ork"


# ── fixtures ────────────────────────────────────────────────────────────────

def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _scoped_run_dir(tmp_path) -> str:
    """A run dir whose declared scope lives under ``mini_ork``.

    Any code path that guesses a region from the run dir would answer
    ``mini_ork``, so a probe that must stay NULL has a wrong answer available
    to catch.
    """
    run_dir = os.path.join(str(tmp_path), "run")
    _write(os.path.join(run_dir, "run_profile.json"), json.dumps({
        "scope_allow": [
            "`mini_ork/dispatch/routing.py`",
            "`mini_ork/trace_store.py`",
            "`docs/architecture.md`",
        ],
    }))
    _write(os.path.join(run_dir, "config", "agents.yaml"),
           "lanes:\n  implementer: sonnet\n  reviewer: opus\n")
    return run_dir


def _state_db(tmp_path, name="state.db") -> str:
    path = os.path.join(str(tmp_path), name)
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE IF NOT EXISTS lane_region_advantage ("
        " task_class TEXT, objective_domain TEXT, code_region TEXT,"
        " node_type TEXT, runs_count INTEGER, relative_advantage REAL,"
        " agent_version_id TEXT, z_score_advantage REAL);"
        "CREATE TABLE IF NOT EXISTS lane_domain_advantage ("
        " task_class TEXT, objective_domain TEXT, node_type TEXT,"
        " runs_count INTEGER, relative_advantage REAL,"
        " agent_version_id TEXT, z_score_advantage REAL);"
        "CREATE TABLE IF NOT EXISTS agent_performance_memory ("
        " task_class TEXT, runs_count INTEGER, role TEXT, model TEXT,"
        " relative_advantage REAL, agent_version_id TEXT);"
        "CREATE TABLE IF NOT EXISTS execution_traces ("
        " trace_id TEXT PRIMARY KEY, run_id TEXT, task_class TEXT,"
        " status TEXT, code_region TEXT);"
        "CREATE TABLE IF NOT EXISTS run_events ("
        " run_id TEXT, node_id TEXT, node_type TEXT, event_type TEXT,"
        " ts TEXT, extra_json TEXT);"
    )
    con.commit()
    con.close()
    return path


def _traces_db(tmp_path, with_code_region=True, name="traces.db") -> str:
    """A trace DB on the post-0044 shape, or pre-0044 without the column."""
    path = os.path.join(str(tmp_path), name)
    con = sqlite3.connect(path)
    region_col = ", code_region TEXT DEFAULT NULL" if with_code_region else ""
    con.execute(
        "CREATE TABLE execution_traces (trace_id TEXT PRIMARY KEY,"
        f" run_id TEXT, task_class TEXT, status TEXT{region_col})")
    con.commit()
    con.close()
    return path


def _rows(db: str) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM execution_traces").fetchall()]
    finally:
        con.close()


def _clean_env(monkeypatch, **kv) -> None:
    for var in ("MO_TARGET_CWD", "MINI_ORK_RUN_DIR", "MINI_ORK_DB",
                "MO_ROUTING_POLICY", "MO_ROUTE_REGION",
                "MINI_ORK_OBJECTIVE_DOMAIN", "MO_OBJECTIVE_DOMAIN",
                "FAIL_COUNT", "MINI_ORK_PROMPT_OVERRIDE_DIR"):
        monkeypatch.delenv(var, raising=False)
    for key, value in kv.items():
        monkeypatch.setenv(key, value)


def _stub_decide(monkeypatch, **overrides) -> None:
    from mini_ork.steering import decision_service
    decision = {
        "route": "sonnet", "route_source": "learned", "route_explore": False,
        "route_score": None, "route_margin": None, "predicted_error": None,
        "code_region": None,
    }
    decision.update(overrides)

    def _decide(node_type, task_class, objective_domain="",
                segment="default", db=None):
        return dict(decision)

    monkeypatch.setattr(decision_service, "decide", _decide)


# ── hop 1: decide() -> _record_route -> last_route_provenance ───────────────

def test_implementer_decision_region_reaches_provenance(tmp_path, monkeypatch):
    run_dir = _scoped_run_dir(tmp_path)
    db = _state_db(tmp_path)
    _clean_env(monkeypatch, MINI_ORK_DB=db, MINI_ORK_RUN_DIR=run_dir, EPSILON="0")

    lane = policy_route_lane("implementer", "implementer", root=None,
                             task_class="framework_edit", run_dir=run_dir)
    prov = last_route_provenance()

    assert lane, "routing produced no lane"
    assert prov.get("route_source"), "the decision was never recorded"
    assert prov.get("code_region") == SCOPE_REGION


def test_provenance_region_is_the_decisions_value_not_a_guess(tmp_path, monkeypatch):
    run_dir = _scoped_run_dir(tmp_path)  # a scope guess would say mini_ork
    db = _state_db(tmp_path)
    _clean_env(monkeypatch, MINI_ORK_DB=db)
    _stub_decide(monkeypatch, code_region="webui")

    policy_route_lane("implementer", "implementer", root=None,
                      task_class="framework_edit", run_dir=run_dir)
    prov = last_route_provenance()

    assert prov.get("route_source") == "learned", "stub decision not recorded"
    assert prov.get("code_region") == "webui"


def test_no_region_substituted_when_decide_returns_none(tmp_path, monkeypatch):
    run_dir = _scoped_run_dir(tmp_path)
    db = _state_db(tmp_path)
    _clean_env(monkeypatch, MINI_ORK_DB=db, MINI_ORK_RUN_DIR=run_dir)
    _stub_decide(monkeypatch, code_region=None, route="opus")

    assert "reviewer" not in REGION_NODE_TYPES
    policy_route_lane("reviewer", "reviewer", root=None,
                      task_class="framework_edit", run_dir=run_dir)
    prov = last_route_provenance()

    assert prov.get("route_source") == "learned", "stub decision not recorded"
    assert prov.get("code_region") is None


# ── hop 2: _tf -> the trace payload / trace_store row ───────────────────────

def test_tf_persists_code_region_to_trace_row(tmp_path, monkeypatch):
    db = _traces_db(tmp_path)
    _clean_env(monkeypatch, MO_PRM_SCORE="0")
    _tf = _make_trace_fn("framework_edit", db, "run-spec")

    _tf("node1", "success", "researcher", "", "", "done",
        lane="sonnet", route_source="learned", route_explore=False,
        route_score=None, route_margin=None, predicted_error=None,
        code_region=SCOPE_REGION)

    rows = _rows(db)
    assert rows, "no execution_traces row was written"
    assert rows[0]["code_region"] == SCOPE_REGION


def test_tf_keeps_column_null_when_decision_had_no_region(tmp_path, monkeypatch):
    db = _traces_db(tmp_path)
    _clean_env(monkeypatch, MO_PRM_SCORE="0", MINI_ORK_RUN_DIR=str(tmp_path))
    _tf = _make_trace_fn("framework_edit", db, "run-spec")

    # A researcher trace with no files_written: infer_trace_code_region has
    # nothing to infer from, so any non-NULL value here was substituted.
    _tf("node2", "success", "researcher", "", "", "done",
        lane="sonnet", route_source="learned", route_explore=False,
        route_score=None, route_margin=None, predicted_error=None,
        code_region=None)

    rows = _rows(db)
    assert rows, "no execution_traces row was written"
    assert rows[0]["code_region"] is None


def test_tf_does_not_stamp_region_without_route_source(tmp_path, monkeypatch):
    db = _traces_db(tmp_path)
    _clean_env(monkeypatch, MO_PRM_SCORE="0")
    _tf = _make_trace_fn("framework_edit", db, "run-spec")

    _tf("node3", "success", "researcher", "", "", "done",
        lane="sonnet", route_source="", route_explore=False,
        route_score=None, route_margin=None, predicted_error=None,
        code_region=SCOPE_REGION)

    rows = _rows(db)
    assert rows, "no execution_traces row was written"
    assert rows[0]["code_region"] is None


def test_files_written_region_override_remains_for_implementer(tmp_path, monkeypatch):
    db = _traces_db(tmp_path)
    _clean_env(monkeypatch, MO_PRM_SCORE="0")
    _tf = _make_trace_fn("framework_edit", db, "run-spec")

    # No decide() region, but the implementer wrote a docs/ file: the
    # files_written-derived UPDATE — not this change — owns the column then.
    _tf("impl1", "success", "implementer", "docs/probe-impl.log", "", "done",
        lane="sonnet", route_source="learned", route_explore=False,
        route_score=None, route_margin=None, predicted_error=None,
        code_region=None)

    rows = _rows(db)
    assert rows, "no execution_traces row was written"
    assert rows[0]["code_region"] == "docs"


# ── hop 3: trace_store write/upsert ─────────────────────────────────────────

def test_trace_store_writes_and_null_rewrite_cannot_blank(tmp_path, monkeypatch):
    db = _traces_db(tmp_path)
    _clean_env(monkeypatch)

    trace_store.trace_write(
        {"trace_id": "tr-spec-1", "task_class": "framework_edit",
         "status": "success", "code_region": SCOPE_REGION}, db=db)
    rows = _rows(db)
    assert rows and rows[0]["code_region"] == SCOPE_REGION

    # A later re-write of the same row without a region must not blank it.
    trace_store.trace_write(
        {"trace_id": "tr-spec-1", "task_class": "framework_edit",
         "status": "failure"}, db=db)
    row = _rows(db)[0]
    assert row["status"] == "failure", "the re-write never landed"
    assert row["code_region"] == SCOPE_REGION

    # …while a later NON-null region still lands: coalesce, not skip.
    trace_store.trace_write(
        {"trace_id": "tr-spec-1", "task_class": "framework_edit",
         "status": "failure", "code_region": "docs"}, db=db)
    assert _rows(db)[0]["code_region"] == "docs"
    assert ("code_region", "coalesce") in trace_store._TRACE_COLUMNS


def test_trace_store_narrows_write_on_pre0044_schema(tmp_path, monkeypatch):
    db = _traces_db(tmp_path, with_code_region=False, name="pre0044.db")
    _clean_env(monkeypatch)

    trace_store.trace_write(
        {"trace_id": "tr-spec-2", "task_class": "framework_edit",
         "status": "success", "code_region": SCOPE_REGION}, db=db)

    rows = _rows(db)
    assert rows and rows[0]["trace_id"] == "tr-spec-2"
