"""Unit tests: dispatcher-crash classification (RB-10) and the missing
``MO_TARGET_CWD`` guard (RB-22b) in ``mini_ork.dispatch.llm_dispatch``.

Hermetic: every test builds its own sqlite ledger in ``tmp_path``, stubs the
dispatcher, and scrubs the run-identity env so nothing touches a live run or
the live DB. No network, no provider credentials.

The defects pinned:

- RB-10: ``classify_error(str(exc))`` over the WHOLE exception text classified
  a ``UnmappedHostPathError`` as ``auth`` — its message embeds the prompt, and
  the code-fix prompt says "forbidden". One strike on a terminal category fused
  the lane for six hours. ``classify_crash`` keys on the exception TYPE.
- RB-22b: a deleted target cwd failed every lane in the chain with
  ``spawn failed: [Errno 2]`` and then rc=127. It is one config row now.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.dispatch import llm_dispatch as ld  # noqa: E402

# Column set = the always-inserted columns of ``write_llm_calls_row`` plus the
# optional ones (error_category/retryable/tokens/cost split) plus the fuse
# query's columns (status, model_id, feature_name, error_category, ts, actor).
_LLM_CALLS_DDL = """
CREATE TABLE llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  provider TEXT,
  model_id TEXT,
  tier TEXT,
  feature_name TEXT,
  actor TEXT,
  status TEXT,
  duration_ms INTEGER,
  cost_usd REAL,
  error_message TEXT,
  iter INTEGER,
  run_id TEXT,
  traceparent TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  total_tokens INTEGER,
  metadata_json TEXT,
  session_id TEXT,
  error_category TEXT,
  retryable INTEGER,
  cached_input_tokens INTEGER,
  cache_creation_input_tokens INTEGER,
  thinking_tokens INTEGER,
  cost_input_uncached_usd REAL,
  cost_input_cached_usd REAL,
  cost_cache_write_usd REAL
)
"""

_SCRUBBED_ENV = ("MINI_ORK_RUN_DIR", "MINI_ORK_RUN_ID", "MINI_ORK_HOME",
                 "MINI_ORK_ROOT", "MO_TARGET_CWD", "MO_LANE_ACTOR",
                 "MO_LANE_TIER", "MO_TRACEPARENT", "MO_RECURSIVE_ITER",
                 "MO_FUSE_WINDOW_HOURS")


def _make_ledger(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_LLM_CALLS_DDL)
    con.commit()
    con.close()
    return str(db)


def _rows(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("SELECT * FROM llm_calls ORDER BY id").fetchall()
    finally:
        con.close()


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """A fresh ledger + scrubbed run-identity env; returns the db path."""
    db = _make_ledger(tmp_path)
    for var in _SCRUBBED_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MINI_ORK_DB", db)
    monkeypatch.setenv("MO_DAILY_BUDGET_USD", "1000000")
    return db


def test_crash_row_is_config_and_excluded_from_lane_fuse(harness, tmp_path):
    """RB-10: a ``ValueError`` whose text says "forbidden … 401" is a CONFIG
    crash (the type decides, not the prompt-embedded text), its actor carries
    the ``:crash`` marker, and that marker keeps it out of the lane fuse."""
    def crashing_dispatch(model, prompt, out_file, timeout_s, max_turns):
        raise ValueError("host path /srv/secret is forbidden (HTTP 401)")

    with pytest.raises(ValueError):
        ld.llm_dispatch(
            ["--node-type", "implementer", "--prompt-text", "fix the bug",
             "--out", str(tmp_path / "out.md"), "--model", "crashlane"],
            dispatch_fn=crashing_dispatch)

    rows = _rows(harness)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "failed"
    assert row["error_category"] == "config"
    assert str(row["actor"]).endswith(":crash")
    # the marker, not a silent miss: this row alone must not open the fuse on
    # the terminal category it was recorded under.
    assert ld.check_lane_fuse(
        harness, "crashlane", "config", strikes=1) is False


def test_classify_crash_connection_error_is_network():
    """RB-10: a connection refusal is a NETWORK crash, never an engine-config
    one — even though its class name ends in ``Error``."""
    assert ld.classify_crash(ConnectionRefusedError()) == "network"


def test_missing_target_cwd_writes_one_config_row_and_returns_127(
        harness, tmp_path, monkeypatch):
    """RB-22b: a set-but-missing target cwd fails ONCE as config, before any
    lane is dispatched — instead of one failure per lane plus rc 127."""
    gone = tmp_path / "gone"  # never created
    monkeypatch.setenv("MO_TARGET_CWD", str(gone))
    calls = []

    def stub(model, prompt, out_file, timeout_s, max_turns):
        calls.append(model)

    rc = ld.llm_dispatch(
        ["--node-type", "implementer", "--prompt-text", "fix the bug",
         "--out", str(tmp_path / "out.md"), "--model", "cwndlane"],
        dispatch_fn=stub)

    assert rc == 127
    assert calls == []
    rows = _rows(harness)
    assert len(rows) == 1
    assert rows[0]["error_category"] == "config"
    assert rows[0]["error_message"] == f"target cwd does not exist: {gone}"
