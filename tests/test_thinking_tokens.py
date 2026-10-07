"""Thinking (reasoning) tokens reach llm_calls.

Claude-CLI result envelopes report usage.output_tokens_details.thinking_tokens;
before 0066 the schema-adaptive writers had no column for it, so consumers saw
0 thinking tokens for every mini-ork call.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from mini_ork.dispatch import DispatchResult, TokenUsage, persist_call
from mini_ork.dispatch.llm_dispatch import write_llm_calls_row
from mini_ork.dispatch.providers import parse_claude_usage

REPO = Path(__file__).resolve().parents[1]

SCHEMA = """
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
CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, applied_at TEXT, checksum TEXT);
"""


def _envelope(**usage_extra) -> str:
    usage = {"input_tokens": 125730, "output_tokens": 20478,
             "cache_read_input_tokens": 181846, "cache_creation_input_tokens": 0, **usage_extra}
    return json.dumps({"type": "result", "result": "ok", "usage": usage,
                       "modelUsage": {"MiniMax-M3": {"thinkingTokens": 13814}}})


def _db(tmp_path: Path) -> Path:
    p = tmp_path / "state.db"
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    con.executescript((REPO / "db/migrations/0066_llm_calls_thinking_tokens.sql").read_text())
    con.commit()
    con.close()
    return p


def test_parses_thinking_from_output_tokens_details():
    usage = parse_claude_usage(_envelope(output_tokens_details={"thinking_tokens": 13814}))
    assert usage.thinking_tokens == 13814
    assert usage.cached_input_tokens == 181846
    assert usage.output_tokens == 20478


def test_falls_back_to_model_usage_thinking_tokens():
    assert parse_claude_usage(_envelope()).thinking_tokens == 13814


def test_no_thinking_reported_is_zero():
    stdout = json.dumps({"type": "result", "usage": {"input_tokens": 1, "output_tokens": 2}})
    assert parse_claude_usage(stdout).thinking_tokens == 0


def test_migration_adds_the_column(tmp_path):
    con = sqlite3.connect(_db(tmp_path))
    cols = {row[1] for row in con.execute("PRAGMA table_info(llm_calls)")}
    assert "thinking_tokens" in cols
    assert con.execute("SELECT filename FROM schema_migrations").fetchone()[0] == "0066_llm_calls_thinking_tokens.sql"


def test_persist_call_writes_thinking_and_cache_tokens(tmp_path):
    db = _db(tmp_path)
    result = DispatchResult(
        ok=True, rc=0, text="ok", model="MiniMax-M3", cost_usd=0.07, duration_ms=1000,
        usage=TokenUsage(input_tokens=125730, output_tokens=20478,
                         cached_input_tokens=181846, thinking_tokens=13814),
    )
    persist_call(db, result, provider="minimax", feature_name="mini-ork:minimax")
    row = sqlite3.connect(db).execute(
        "SELECT thinking_tokens, cached_input_tokens, output_tokens FROM llm_calls").fetchone()
    assert row == (13814, 181846, 20478)


def test_write_llm_calls_row_writes_thinking_tokens(tmp_path):
    db = _db(tmp_path)
    write_llm_calls_row(str(db), "minimax", "MiniMax-M3", "default", "mini-ork:minimax", "minimax",
                        "success", 1000, 0.07, "", 125730, 20478, "{}", 181846, 0,
                        thinking_tokens=13814)
    row = sqlite3.connect(db).execute("SELECT thinking_tokens FROM llm_calls").fetchone()
    assert row == (13814,)
