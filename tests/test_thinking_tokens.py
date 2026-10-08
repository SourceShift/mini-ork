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


def test_no_thinking_reported_is_none_not_zero():
    # A provider envelope that reports no thinking figure at all must be
    # distinguishable from one that reported 0 — the absence is None.
    stdout = json.dumps({"type": "result", "usage": {"input_tokens": 1, "output_tokens": 2}})
    assert parse_claude_usage(stdout).thinking_tokens is None


def test_reported_zero_is_kept_distinct_from_unreported():
    # thinking_tokens: 0 is a MEASUREMENT ("did no thinking") and stays 0;
    # a bare envelope with no figure is None ("not measured").
    reported = _envelope(output_tokens_details={"thinking_tokens": 0})
    assert parse_claude_usage(reported).thinking_tokens == 0
    bare = json.dumps({"type": "result", "usage": {"input_tokens": 1, "output_tokens": 2}})
    assert parse_claude_usage(bare).thinking_tokens is None


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


def test_unreported_thinking_is_stored_as_null(tmp_path):
    # The bug: an unreported figure was coerced to 0, indistinguishable from a
    # real zero. Both write paths must store NULL when nothing was reported.
    db = _db(tmp_path)
    write_llm_calls_row(str(db), "codex", "gpt-5", "default", "mini-ork:codex", "codex",
                        "success", 1000, 0.02, "", 100, 20, "{}")
    row = sqlite3.connect(db).execute("SELECT thinking_tokens FROM llm_calls").fetchone()
    assert row == (None,)


def test_persist_call_stores_null_when_usage_reports_no_thinking(tmp_path):
    db = _db(tmp_path)
    result = DispatchResult(
        ok=True, rc=0, text="ok", model="gpt-5", cost_usd=0.02, duration_ms=500,
        usage=TokenUsage(input_tokens=100, output_tokens=20),
    )
    persist_call(db, result, provider="codex", feature_name="mini-ork:codex")
    row = sqlite3.connect(db).execute("SELECT thinking_tokens FROM llm_calls").fetchone()
    assert row == (None,)


def test_migration_0067_drops_the_thinking_default(tmp_path):
    """The absence of a thinking figure must land as SQL NULL, so the column
    must not carry a DEFAULT that fabricates a 0 for a writer that omits it."""
    import shutil

    from mini_ork.stores import migrate

    mig = tmp_path / "migrations"
    mig.mkdir()
    real = REPO / "db/migrations"
    for f in sorted(real.glob("*.sql")):
        if int(f.name[:4]) <= 66:  # the chain up to and including the column's birth
            shutil.copy(f, mig / f.name)
    db = str(tmp_path / "state.db")
    rc, out = migrate.migrate_apply(str(mig), db=db, root=str(REPO))
    assert rc == 0, out

    con = sqlite3.connect(db)
    before = {r[1]: r[4] for r in con.execute("PRAGMA table_info(llm_calls)")}
    assert before["thinking_tokens"] == "0"  # 0066's default — the schema-level bug
    con.execute("INSERT INTO llm_calls (provider,model_id,tier,feature_name,status,thinking_tokens)"
                " VALUES ('m','M','default','f','success',7)")
    con.commit()
    con.close()

    shutil.copy(real / "0067_llm_calls_thinking_tokens_nullable.sql",
                mig / "0067_llm_calls_thinking_tokens_nullable.sql")
    rc, out = migrate.migrate_apply(str(mig), db=db, root=str(REPO))
    assert rc == 0, out

    con = sqlite3.connect(db)
    after = {r[1]: r[4] for r in con.execute("PRAGMA table_info(llm_calls)")}
    assert after["thinking_tokens"] is None
    # the rebuild preserves existing rows (measured 7 stays 7)
    assert con.execute("SELECT thinking_tokens FROM llm_calls").fetchone()[0] == 7
    # and a writer that omits the column now gets NULL, not a fabricated 0
    con.execute("INSERT INTO llm_calls (provider,model_id,tier,feature_name,status)"
                " VALUES ('n','N','default','f','success')")
    con.commit()
    assert con.execute(
        "SELECT thinking_tokens FROM llm_calls WHERE provider='n'").fetchone()[0] is None
