"""Unit tests: the RouteMoA screen reads the routed slice's own outcomes.

Hermetic: every test builds its own sqlite DB under ``tmp_path``. The screen
must count real ``status='success'`` rows over the ``(task_class, node_type,
objective_domain)`` slice of ``execution_traces`` instead of
``agent_performance_memory``'s groups-won ``success_count``.
"""
from __future__ import annotations

import itertools
import json
import sqlite3

import pytest

import mini_ork.lane_router as lane_router
from mini_ork.learning.advantage_store import AdvantageStore

TC = "slice-stats-tc"
OD = "od1"
CREATED_AT = "2026-10-01T00:00:00.000Z"

TRACE_DDL = """CREATE TABLE IF NOT EXISTS execution_traces (
    trace_id TEXT, agent_version_id TEXT, task_class TEXT, objective_domain TEXT,
    code_region TEXT, verifier_output TEXT, reward_g REAL, cost_usd REAL,
    status TEXT, created_at TEXT, validity TEXT)"""

TRACE_DDL_NO_VALIDITY = """CREATE TABLE IF NOT EXISTS execution_traces (
    trace_id TEXT, agent_version_id TEXT, task_class TEXT, objective_domain TEXT,
    code_region TEXT, verifier_output TEXT, reward_g REAL, cost_usd REAL,
    status TEXT, created_at TEXT)"""

APM_DDL = """CREATE TABLE IF NOT EXISTS agent_performance_memory (
    agent_version_id TEXT, role TEXT, model TEXT, task_class TEXT,
    runs_count INTEGER, success_count INTEGER, relative_advantage REAL,
    last_updated TEXT, PRIMARY KEY (agent_version_id, task_class))"""

_TRACE_SEQ = itertools.count()


@pytest.fixture(autouse=True)
def _router_env(monkeypatch):
    """Pin every knob the screen and ``_pick_lane`` read."""
    for var in ("MO_ROUTEMOA", "MO_ROUTEMOA_MIN_RUNS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MO_LEARNING_MIN_SAMPLES", "3")
    monkeypatch.setenv("MO_ROUTER_UCB_C", "0.5")
    monkeypatch.setenv("MO_ROUTER_CONTEXTUAL", "0")
    monkeypatch.setenv("MO_EQUIROUTER", "1")
    monkeypatch.setenv("MO_ENTROROUTER", "1")
    monkeypatch.setenv("MO_ROUTER_ANCHOR", "0.5")
    monkeypatch.setenv("MO_ROUTER_ENTROPY_TAU", "3")


def _nt(node_type):
    return json.dumps({"node_type": node_type})


def _insert(con, lane, *, status="success", reward_g=1.0, n=1,
            node_type="implementer", task_class=TC, objective_domain=OD,
            validity=None, verifier_output=None, with_validity=True):
    """Insert ``n`` identical trace rows for ``lane`` (unique ``trace_id``)."""
    vo = _nt(node_type) if verifier_output is None else verifier_output
    cols = ("trace_id, agent_version_id, task_class, objective_domain, "
            "code_region, verifier_output, reward_g, cost_usd, status, created_at")
    if with_validity:
        cols += ", validity"
    placeholders = ",".join("?" * (11 if with_validity else 10))
    for _ in range(n):
        vals = [f"t{next(_TRACE_SEQ)}", lane, task_class, objective_domain, "",
                vo, reward_g, 0.0, status, CREATED_AT]
        if with_validity:
            vals.append(validity)
        con.execute(
            f"INSERT INTO execution_traces ({cols}) VALUES ({placeholders})", vals)


def _big_slice_db(path):
    """The kickoff's seeded slice: every exclusion the SQL must apply."""
    con = sqlite3.connect(str(path))
    con.execute(TRACE_DDL)
    # lane-a / implementer / od1: 9 success (validity NULL) + 1 failure (valid).
    _insert(con, "lane-a", status="success", reward_g=1.0, n=9, validity=None)
    _insert(con, "lane-a", status="failure", reward_g=-1.0, n=1, validity="valid")
    # lane-b / implementer / od1: 2 success + 8 failure.
    _insert(con, "lane-b", status="success", reward_g=1.0, n=2, validity=None)
    _insert(con, "lane-b", status="failure", reward_g=-1.0, n=8, validity=None)
    # Wrong node_type.
    _insert(con, "lane-a", node_type="reviewer", status="failure", reward_g=-1.0, n=5)
    # Wrong objective_domain.
    _insert(con, "lane-a", objective_domain="od2", status="failure",
            reward_g=-1.0, n=5)
    # No reward: not a reward-bearing row.
    _insert(con, "lane-a", status="failure", reward_g=None, n=3)
    # No lane id.
    _insert(con, "", status="success", reward_g=1.0, n=2)
    # Invalid verdict row.
    _insert(con, "lane-a", status="failure", reward_g=-1.0, n=4,
            validity="infra_failed")
    # Wrong task_class.
    _insert(con, "lane-b", task_class="other-tc", status="success",
            reward_g=1.0, n=10)
    con.commit()
    con.close()


def _fetch(path, *args):
    store = AdvantageStore(str(path)).open()
    try:
        return store.fetch_lane_run_stats(*args)
    finally:
        store.close()


def test_real_successes_for_the_slice(tmp_path):
    path = tmp_path / "slice.db"
    _big_slice_db(path)
    assert _fetch(path, TC, "implementer", OD) == {
        "lane-a": (9, 10), "lane-b": (2, 10)}


def test_empty_filters_widen_the_slice(tmp_path):
    path = tmp_path / "slice.db"
    _big_slice_db(path)
    assert _fetch(path, TC) == {"lane-a": (9, 20), "lane-b": (2, 10)}
    assert _fetch(path, TC, "", OD) == {"lane-a": (9, 15), "lane-b": (2, 10)}
    assert _fetch(path, TC, "reviewer", OD) == {"lane-a": (0, 5)}


def test_node_type_parsing_matches_recompute(tmp_path):
    path = tmp_path / "parse.db"
    con = sqlite3.connect(str(path))
    con.execute(TRACE_DDL)
    outputs = [
        "not json",
        '{"node_type": ""}',
        json.dumps('{"node_type": "implementer"}'),
        None,
    ]
    for i, vo in enumerate(outputs):
        con.execute(
            "INSERT INTO execution_traces (trace_id, agent_version_id, task_class,"
            " objective_domain, code_region, verifier_output, reward_g, cost_usd,"
            " status, created_at, validity) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [f"t{i}", "lane-c", TC, OD, "", vo, 1.0, 0.0, "success", CREATED_AT, None])
    con.commit()
    con.close()
    assert _fetch(path, TC, "unknown", OD) == {"lane-c": (4, 4)}
    assert _fetch(path, TC, "implementer", OD) == {}


def test_no_validity_column_is_fine(tmp_path):
    path = tmp_path / "noval.db"
    con = sqlite3.connect(str(path))
    con.execute(TRACE_DDL_NO_VALIDITY)
    _insert(con, "lane-d", status="success", reward_g=1.0, n=3, with_validity=False)
    con.commit()
    con.close()
    assert _fetch(path, TC, "implementer", OD) == {"lane-d": (3, 3)}


def test_missing_trace_table_raises(tmp_path):
    path = tmp_path / "apm-only.db"
    con = sqlite3.connect(str(path))
    con.execute(APM_DDL)
    con.execute("INSERT INTO agent_performance_memory VALUES"
                " ('lane-x', 'implementer', 'm', ?, 50, 50, 0.5, ?)",
                [TC, CREATED_AT])
    con.commit()
    con.close()
    with pytest.raises(sqlite3.OperationalError, match="execution_traces"):
        _fetch(path, TC)


def _make_pick_db(path):
    """APM groups-won rows that would screen lane-hi out under the old read;
    traces that rule lane-lo out instead."""
    con = sqlite3.connect(str(path))
    con.execute(TRACE_DDL)
    _insert(con, "lane-hi", status="success", reward_g=1.0, n=380)
    _insert(con, "lane-hi", status="failure", reward_g=-1.0, n=20)
    _insert(con, "lane-lo", status="success", reward_g=1.0, n=80)
    _insert(con, "lane-lo", status="failure", reward_g=-1.0, n=15)
    _insert(con, "lane-hi", node_type="reviewer", status="failure",
            reward_g=-1.0, n=50)
    con.commit()
    con.close()

    store = AdvantageStore(str(path)).open()
    try:
        store.ensure_advantage_tables()
        store.con.execute(APM_DDL)
        store.upsert_agent_performance("lane-hi", "implementer", "m1", TC, 400, 3, 0.0)
        store.upsert_agent_performance("lane-lo", "implementer", "m2", TC, 95, 8, 0.0)
        store.upsert_domain_advantage(
            "lane-hi", TC, "implementer", OD, 0.05, 400, 3, 0.0, 0.0, 0.5)
        store.upsert_domain_advantage(
            "lane-lo", TC, "implementer", OD, 0.02, 95, 8, 0.0, 0.0, 0.1)
        store.commit()
    finally:
        store.close()


def test_pick_screens_on_slice_outcomes_not_apm_groups_won(tmp_path):
    path = tmp_path / "pick.db"
    _make_pick_db(str(path))
    detail = lane_router._pick_lane(TC, "implementer", OD, "", str(path))
    assert detail["lane"] == "lane-hi", detail
    assert detail["source"] == "domain", detail
    assert [e["lane"] for e in detail["screened"]] == ["lane-lo"], detail
    assert detail["screened"][0]["dominated_by"] == "lane-hi", detail


def test_opting_out_still_works(tmp_path, monkeypatch):
    path = tmp_path / "pick.db"
    _make_pick_db(str(path))
    monkeypatch.setenv("MO_ROUTEMOA", "0")
    detail = lane_router._pick_lane(TC, "implementer", OD, "", str(path))
    assert detail["lane"] == "lane-hi", detail
    assert detail["screened"] == [], detail
