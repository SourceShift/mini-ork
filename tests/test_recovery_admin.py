"""E5: recovery admin — DAG projection + `recover --cancel` (leaves checkpoints).

Function names carry ``recovery_ui`` so they are collected by the E5 gate
``pytest -k "trace or recovery_ui"``.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mini_ork.recovery import admin as ra

SCHEMA = """
CREATE TABLE node_checkpoints(run_id TEXT,node_id TEXT,attempt INT DEFAULT 1,status TEXT,
 input_hash TEXT,recipe_version TEXT,config_hash TEXT,artifact_manifest_json TEXT,
 session_ref TEXT,failure_class TEXT,created_at INT,PRIMARY KEY(run_id,node_id));
CREATE TABLE node_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,node_id TEXT,
 attempt_no INT,node_type TEXT,started_at INT,ended_at INT,result TEXT,failure_class TEXT,
 checkpoint_used INT DEFAULT 0,checkpoint_produced INT DEFAULT 0,cost_usd REAL,
 provider_session_id TEXT,initiator TEXT);
CREATE TABLE run_leases(run_id TEXT PRIMARY KEY,owner_token TEXT NOT NULL,acquired_at INT,
 expires_at INT,renewed_at INT);
CREATE TABLE recovery_requests(request_id TEXT PRIMARY KEY,run_id TEXT,from_node TEXT,
 strategy TEXT,status TEXT,failure_class TEXT,budget_usd REAL DEFAULT 5,cost_usd REAL DEFAULT 0,
 dispatch_count INT DEFAULT 0,owner_token TEXT,created_at INT,last_dispatched_at INT,
 closed_at INT,payload_json TEXT);
"""


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return str(p)


def _ck(con, run_id, node_id, status):
    con.execute("INSERT INTO node_checkpoints(run_id,node_id,attempt,status,input_hash,"
                "recipe_version,config_hash,artifact_manifest_json,created_at) "
                "VALUES(?,?,1,?,'ih','rv','ch','[]',1000)", (run_id, node_id, status))


def _att(con, run_id, node_id, attempt_no, result, fc=None):
    con.execute("INSERT INTO node_attempts(run_id,node_id,attempt_no,node_type,started_at,"
                "ended_at,result,failure_class) VALUES(?,?,?,'implementer',1,2,?,?)",
                (run_id, node_id, attempt_no, result, fc))


# ── projection ─────────────────────────────────────────────────────────────
def test_recovery_ui_projection_shows_completed_and_failed(db_path):
    con = sqlite3.connect(db_path)
    for n in ("A", "B"):
        _ck(con, "r1", n, "success")
        _att(con, "r1", n, 1, "success")
    _att(con, "r1", "C", 1, "failure", "provider_limit")   # C failed, no checkpoint
    con.commit(); con.close()

    view = ra.recovery_projection(db_path, "r1")
    byid = {n["node_id"]: n for n in view["nodes"]}
    assert byid["A"]["status"] == "success" and byid["A"]["reusable"] is True
    assert byid["C"]["status"] == "failure" and byid["C"]["reusable"] is False
    assert byid["C"]["attempts"][0]["failure_class"] == "provider_limit"
    assert "C" in view["next_action"]


def test_recovery_ui_projection_nests_multiple_attempts(db_path):
    con = sqlite3.connect(db_path)
    _att(con, "r1", "C", 1, "failure", "infra_interrupt")
    _att(con, "r1", "C", 2, "failure", "provider_limit")
    con.commit(); con.close()
    view = ra.recovery_projection(db_path, "r1")
    c = [n for n in view["nodes"] if n["node_id"] == "C"][0]
    assert len(c["attempts"]) == 2                       # nested under ONE node
    assert [a["attempt_no"] for a in c["attempts"]] == [1, 2]


def test_recovery_ui_projection_active_recovery_and_lease(db_path):
    con = sqlite3.connect(db_path)
    _ck(con, "r1", "A", "success")
    con.execute("INSERT INTO run_leases(run_id,owner_token,acquired_at,expires_at,renewed_at) "
                "VALUES('r1','tok',1,9999999999,1)")
    con.execute("INSERT INTO recovery_requests(request_id,run_id,from_node,strategy,status,"
                "dispatch_count,owner_token,created_at) VALUES('req1','r1','C','resume','dispatched',1,'tok',1000)")
    con.commit(); con.close()
    view = ra.recovery_projection(db_path, "r1")
    assert view["active_recovery"]["request_id"] == "req1"
    assert view["active_recovery"]["from_node"] == "C"
    assert view["lease"]["owner_token"] == "tok" and view["lease"]["live"] is True
    assert "in progress" in view["next_action"]


# ── cancel ─────────────────────────────────────────────────────────────────
def test_recovery_ui_cancel_leaves_checkpoints_valid(db_path):
    con = sqlite3.connect(db_path)
    _ck(con, "r1", "A", "success")
    _ck(con, "r1", "B", "success")
    con.execute("INSERT INTO run_leases(run_id,owner_token,acquired_at,expires_at,renewed_at) "
                "VALUES('r1','tok',1,9999999999,1)")
    con.execute("INSERT INTO recovery_requests(request_id,run_id,from_node,strategy,status,"
                "owner_token,created_at) VALUES('req1','r1','C','resume','dispatched','tok',1000)")
    con.commit(); con.close()

    res = ra.cancel_recovery(db_path, "req1", now=2000)
    assert res["ok"] is True
    assert res["previous_status"] == "dispatched"
    assert res["lease_released"] is True

    con = sqlite3.connect(db_path)
    # checkpoints untouched — the acceptance
    ck = con.execute("SELECT COUNT(*) FROM node_checkpoints WHERE run_id='r1' AND status='success'").fetchone()[0]
    req = con.execute("SELECT status, failure_class FROM recovery_requests WHERE request_id='req1'").fetchone()
    lease = con.execute("SELECT COUNT(*) FROM run_leases WHERE run_id='r1'").fetchone()[0]
    con.close()
    assert ck == 2                                   # prior checkpoints still valid
    assert req == ("failed", "cancelled")            # recovery marked cancelled
    assert lease == 0                                # lease released → a fresh recovery can acquire


def test_recovery_ui_cancel_idempotent_on_closed(db_path):
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO recovery_requests(request_id,run_id,from_node,strategy,status,created_at) "
                "VALUES('req1','r1','C','resume','completed',1000)")
    con.commit(); con.close()
    res = ra.cancel_recovery(db_path, "req1")
    assert res["ok"] is True and res["previous_status"] == "completed"


def test_recovery_ui_cancel_unknown_request_fails(db_path):
    res = ra.cancel_recovery(db_path, "nope")
    assert res["ok"] is False
