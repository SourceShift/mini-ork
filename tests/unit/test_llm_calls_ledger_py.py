import sqlite3

from mini_ork.ported.llm_dispatch import write_llm_calls_row


def test_native_writer_persists_success_row_and_optional_cost_columns(tmp_path, monkeypatch):
    db = tmp_path / "calls.db"
    with sqlite3.connect(db) as con:
        con.execute("""CREATE TABLE llm_calls (
            provider TEXT, model_id TEXT, tier TEXT, feature_name TEXT, actor TEXT,
            status TEXT, duration_ms INTEGER, cost_usd REAL, error_message TEXT,
            input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
            metadata_json TEXT, cached_input_tokens INTEGER,
            cache_creation_input_tokens INTEGER, cost_input_uncached_usd REAL,
            cost_input_cached_usd REAL, cost_cache_write_usd REAL,
            iter INTEGER, run_id TEXT, traceparent TEXT, session_id TEXT
        )""")
    monkeypatch.setenv("MINI_ORK_RUN_ID", "34")
    monkeypatch.setenv("MO_RECURSIVE_ITER", "34")
    write_llm_calls_row(str(db), "anthropic", "sonnet", "default", "mini-ork:test",
                        "researcher", "success", 1234, 0.0021, "", 100, 20,
                        '{"session_id":"s1"}', 10, 5)
    with sqlite3.connect(db) as con:
        row = con.execute("SELECT status, duration_ms, actor, cost_usd, total_tokens, session_id "
                          "FROM llm_calls").fetchone()
    assert row == ("success", 1234, "researcher", 0.0021, 120, "s1")
