"""E4: tool-call receipts — a completed side-effecting tool is not re-invoked
on replay (scenario 8).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mini_ork.stores import tool_receipts as tr

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_receipts (
    receipt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, node_id TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1, tool_name TEXT NOT NULL, input_hash TEXT NOT NULL,
    idempotent INTEGER NOT NULL DEFAULT 0, output_json TEXT,
    status TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('completed','failed')),
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tool_receipts
    ON tool_receipts(run_id, node_id, tool_name, input_hash);
"""


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    con.commit()
    con.close()
    return str(p)


def test_record_and_get_receipt_roundtrip(db_path):
    rid = tr.record_receipt(db_path, "r1", "impl", "git_commit",
                            {"paths": ["a.py"]}, {"sha": "abc123"}, now=1000)
    assert rid
    got = tr.get_receipt(db_path, "r1", "impl", "git_commit", {"paths": ["a.py"]})
    assert got["output"] == {"sha": "abc123"}
    assert got["status"] == "completed"
    assert got["idempotent"] is False


def test_receipt_input_hash_is_order_stable(db_path):
    tr.record_receipt(db_path, "r1", "impl", "post", {"a": 1, "b": 2}, "ok", now=1000)
    # same dict, keys in a different order → same receipt
    got = tr.get_receipt(db_path, "r1", "impl", "post", {"b": 2, "a": 1})
    assert got is not None and got["output"] == "ok"


# ── scenario 8: side-effecting tool already ran → NOT re-invoked ────────────
def test_receipt_replay_does_not_reinvoke_side_effect(db_path):
    calls = {"n": 0}

    def do_commit():
        calls["n"] += 1
        return {"sha": f"commit-{calls['n']}"}

    tool_input = {"paths": ["x.py"], "msg": "fix"}

    # first run: no receipt → invoke once, record
    r1 = tr.replay_or_invoke(db_path, "r1", "impl", "git_commit", tool_input, do_commit)
    assert r1 == {"sha": "commit-1"}
    assert calls["n"] == 1

    # replay (recovery): receipt exists + non-idempotent → return it, DO NOT re-invoke
    r2 = tr.replay_or_invoke(db_path, "r1", "impl", "git_commit", tool_input, do_commit)
    assert r2 == {"sha": "commit-1"}     # same receipt, not commit-2
    assert calls["n"] == 1               # ← the side effect fired exactly once


def test_receipt_readonly_tool_replays_fresh(db_path):
    calls = {"n": 0}

    def do_read():
        calls["n"] += 1
        return f"read-{calls['n']}"

    ti = {"path": "conf.yaml"}
    r1 = tr.replay_or_invoke(db_path, "r1", "impl", "read_file", ti, do_read, idempotent=True)
    r2 = tr.replay_or_invoke(db_path, "r1", "impl", "read_file", ti, do_read, idempotent=True)
    # read-only → re-invoked each time (result may have changed)
    assert r1 == "read-1" and r2 == "read-2"
    assert calls["n"] == 2


def test_receipt_distinct_inputs_are_distinct(db_path):
    tr.record_receipt(db_path, "r1", "impl", "git_commit", {"paths": ["a"]}, "sha-a", now=1000)
    tr.record_receipt(db_path, "r1", "impl", "git_commit", {"paths": ["b"]}, "sha-b", now=1000)
    assert tr.get_receipt(db_path, "r1", "impl", "git_commit", {"paths": ["a"]})["output"] == "sha-a"
    assert tr.get_receipt(db_path, "r1", "impl", "git_commit", {"paths": ["b"]})["output"] == "sha-b"


def test_receipt_upsert_on_repeat(db_path):
    tr.record_receipt(db_path, "r1", "impl", "post", {"k": 1}, "v1", now=1000)
    tr.record_receipt(db_path, "r1", "impl", "post", {"k": 1}, "v2", now=1001)  # same key
    con = sqlite3.connect(db_path)
    n = con.execute("SELECT COUNT(*) FROM tool_receipts WHERE run_id='r1'").fetchone()[0]
    con.close()
    assert n == 1                        # UPSERT, not duplicate
    assert tr.get_receipt(db_path, "r1", "impl", "post", {"k": 1})["output"] == "v2"
