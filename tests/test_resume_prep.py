"""E4: resume preparation — bridge durable state → MO_RESUME_SESSION_ID.

Covers: reading a node's (session_id, session_ref) from durable state,
restoring the transcript, returning the id to `--resume`; and the codex
node-level fallback (returns "").
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mini_ork.recovery import resume_prep as rpre
from mini_ork.stores import session_store as ss

FAKE_CWD = "/work/proj-y"

SCHEMA = """
CREATE TABLE IF NOT EXISTS node_checkpoints (
    run_id TEXT NOT NULL, node_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL, input_hash TEXT NOT NULL, recipe_version TEXT NOT NULL,
    config_hash TEXT NOT NULL, artifact_manifest_json TEXT NOT NULL, session_ref TEXT,
    failure_class TEXT, created_at INTEGER NOT NULL, PRIMARY KEY (run_id, node_id)
);
CREATE TABLE IF NOT EXISTS node_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, node_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL, node_type TEXT, started_at INTEGER NOT NULL,
    ended_at INTEGER NOT NULL, result TEXT NOT NULL, failure_class TEXT,
    checkpoint_used INTEGER NOT NULL DEFAULT 0, checkpoint_produced INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL, provider_session_id TEXT, initiator TEXT
);
"""


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    home = tmp_path / "claudehome"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    (home / "projects").mkdir(parents=True)
    return home


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return str(p)


def _seed_node(db_path, run_id, node_id, session_id, session_ref):
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO node_checkpoints(run_id,node_id,attempt,status,input_hash,"
        "recipe_version,config_hash,artifact_manifest_json,session_ref,created_at) "
        "VALUES(?,?,1,'success','ih','rv','ch','[]',?,1000)",
        (run_id, node_id, session_ref))
    con.execute(
        "INSERT INTO node_attempts(run_id,node_id,attempt_no,node_type,started_at,"
        "ended_at,result,provider_session_id) VALUES(?,?,1,'implementer',1000,1001,'failure',?)",
        (run_id, node_id, session_id))
    con.commit()
    con.close()


def test_session_ref_reads_id_and_ref(db_path):
    _seed_node(db_path, "r1", "critic", "sess-77", "sessions/sess-77.jsonl")
    sid, ref = rpre.node_session_ref(db_path, "r1", "critic")
    assert sid == "sess-77"
    assert ref == "sessions/sess-77.jsonl"


def test_session_ref_empty_for_unknown_node(db_path):
    assert rpre.node_session_ref(db_path, "r1", "ghost") == ("", "")


def test_prepare_resume_turn_restores_and_returns_id(db_path, claude_home, tmp_path):
    run_dir = tmp_path / "run"; (run_dir / "sessions").mkdir(parents=True)
    (run_dir / "sessions" / "sess-77.jsonl").write_text('{"turn":9}\n')
    _seed_node(db_path, "r1", "critic", "sess-77", "sessions/sess-77.jsonl")

    sid = rpre.prepare_node_resume(db_path, "r1", "critic", run_dir=str(run_dir),
                                   model="minimax", cwd=FAKE_CWD)
    assert sid == "sess-77"
    # the transcript was restored into the (fake) claude home
    assert ss.find_session_jsonl("sess-77", cwd=FAKE_CWD) is not None


def test_prepare_resume_turn_codex_falls_back_to_node_level(db_path, claude_home, tmp_path):
    run_dir = tmp_path / "run"; (run_dir / "sessions").mkdir(parents=True)
    (run_dir / "sessions" / "sess-77.jsonl").write_text("{}\n")
    _seed_node(db_path, "r1", "critic", "sess-77", "sessions/sess-77.jsonl")
    # codex lane → no turn-resume (its own session model)
    assert rpre.prepare_node_resume(db_path, "r1", "critic", run_dir=str(run_dir),
                                    model="codex", cwd=FAKE_CWD) == ""
    assert rpre.prepare_node_resume(db_path, "r1", "critic", run_dir=str(run_dir),
                                    model="codex_lens", cwd=FAKE_CWD) == ""


def test_prepare_resume_turn_empty_when_no_session(db_path, claude_home, tmp_path):
    run_dir = tmp_path / "run"; run_dir.mkdir()
    # node exists but never captured a session id
    _seed_node(db_path, "r1", "impl", "", "")
    assert rpre.prepare_node_resume(db_path, "r1", "impl", run_dir=str(run_dir),
                                    model="minimax", cwd=FAKE_CWD) == ""
