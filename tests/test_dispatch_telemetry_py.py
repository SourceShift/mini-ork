"""Tests for mini_ork.dispatch.telemetry — persisting a DispatchResult to
llm_calls. Faithful-port checks: column introspection (works on old schemas),
cache-aware cost split, success/failed status, and no-op on a missing DB.
"""

from __future__ import annotations

import sqlite3

import pytest

from mini_ork.dispatch import (
    DispatchResult,
    TokenUsage,
    cache_aware_cost,
    persist_call,
)

# A faithful slice of the llm_calls schema (core + the 0024 cache-aware columns).
FULL_SCHEMA = """
CREATE TABLE llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL, model_id TEXT NOT NULL, tier TEXT NOT NULL,
  feature_name TEXT NOT NULL, actor TEXT, run_id INTEGER, iter INTEGER,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('success','failed')),
  error_message TEXT, traceparent TEXT, session_id TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  cached_input_tokens INTEGER DEFAULT 0, cache_creation_input_tokens INTEGER DEFAULT 0,
  cost_input_uncached_usd REAL DEFAULT 0, cost_input_cached_usd REAL DEFAULT 0,
  cost_cache_write_usd REAL DEFAULT 0,
  ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""

# An OLD schema missing every additive cache column — proves introspection.
MINIMAL_SCHEMA = """
CREATE TABLE llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL, model_id TEXT NOT NULL, tier TEXT NOT NULL,
  feature_name TEXT NOT NULL, cost_usd REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('success','failed')),
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
"""


def _db(tmp_path, schema):
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(schema)
    con.commit()
    con.close()
    return p


def _ok_result():
    return DispatchResult(
        ok=True,
        rc=0,
        text="hi",
        model="glm",
        usage=TokenUsage(input_tokens=1000, output_tokens=200, cached_input_tokens=300),
        cost_usd=0.05,
        duration_ms=1234,
    )


def test_persist_success_row(tmp_path):
    db = _db(tmp_path, FULL_SCHEMA)
    rowid = persist_call(
        db, _ok_result(), provider="anthropic", feature_name="mini-ork:implementer",
        tier="smart", actor="glm", run_id="run-1", session_id="sess-9",
    )
    assert rowid is not None
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT provider, model_id, status, input_tokens, output_tokens, "
        "total_tokens, cost_usd, cached_input_tokens, session_id, metadata_json "
        "FROM llm_calls WHERE id=?",
        (rowid,),
    ).fetchone()
    con.close()
    assert row[0] == "anthropic"
    assert row[1] == "glm"
    assert row[2] == "success"
    assert row[3] == 1000 and row[4] == 200
    assert row[5] == 1200  # total = input + output
    assert row[6] == pytest.approx(0.05)
    assert row[7] == 300
    assert row[8] == "sess-9"
    assert '"session_id": "sess-9"' in row[9]  # folded into metadata too


def test_persist_failed_row_records_error(tmp_path):
    db = _db(tmp_path, FULL_SCHEMA)
    res = DispatchResult(ok=False, rc=7, error="rate limit reached", model="codex")
    rowid = persist_call(db, res, provider="openai", feature_name="mini-ork:planner")
    con = sqlite3.connect(db)
    status, err = con.execute(
        "SELECT status, error_message FROM llm_calls WHERE id=?", (rowid,)
    ).fetchone()
    con.close()
    assert status == "failed"
    assert err == "rate limit reached"


def test_cost_breakdown_persisted(tmp_path):
    db = _db(tmp_path, FULL_SCHEMA)
    res = DispatchResult(
        ok=True, rc=0, model="glm",
        usage=TokenUsage(input_tokens=1000, cached_input_tokens=200, cache_creation_tokens=100),
    )
    rowid = persist_call(db, res, provider="anthropic", feature_name="f")
    con = sqlite3.connect(db)
    uncached, cached, cw = con.execute(
        "SELECT cost_input_uncached_usd, cost_input_cached_usd, cost_cache_write_usd "
        "FROM llm_calls WHERE id=?",
        (rowid,),
    ).fetchone()
    con.close()
    # uncached_in = 1000-200-100 = 700
    assert uncached == pytest.approx(700 * 15.0 / 1_000_000)
    assert cached == pytest.approx(200 * 1.5 / 1_000_000)
    assert cw == pytest.approx(100 * 18.75 / 1_000_000)


def test_introspection_writes_only_existing_columns(tmp_path):
    # Old schema with NO cache columns — must still insert the core row, not crash.
    db = _db(tmp_path, MINIMAL_SCHEMA)
    rowid = persist_call(db, _ok_result(), provider="anthropic", feature_name="f")
    assert rowid is not None
    con = sqlite3.connect(db)
    provider, status = con.execute(
        "SELECT provider, status FROM llm_calls WHERE id=?", (rowid,)
    ).fetchone()
    con.close()
    assert provider == "anthropic" and status == "success"


def test_missing_db_is_noop_not_crash(tmp_path):
    assert persist_call(tmp_path / "nope.db", _ok_result(), provider="x", feature_name="f") is None


def test_cache_aware_cost_subtracts_cached_and_creation():
    u = TokenUsage(input_tokens=1000, cached_input_tokens=200, cache_creation_tokens=100)
    uncached, cached, cw = cache_aware_cost(u)
    assert uncached == pytest.approx(700 * 15.0 / 1_000_000)
    assert cached == pytest.approx(200 * 1.5 / 1_000_000)
    assert cw == pytest.approx(100 * 18.75 / 1_000_000)
