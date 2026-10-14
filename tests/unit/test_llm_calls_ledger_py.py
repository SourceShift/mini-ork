import sqlite3

from mini_ork.dispatch.llm_dispatch import write_llm_calls_row


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


_LEDGER_DDL = """CREATE TABLE llm_calls (
    provider TEXT, model_id TEXT, tier TEXT, feature_name TEXT, actor TEXT,
    status TEXT, duration_ms INTEGER, cost_usd REAL, error_message TEXT,
    input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
    metadata_json TEXT, iter INTEGER, run_id TEXT, traceparent TEXT, session_id TEXT
)"""


def _locked_ledger(tmp_path):
    """A WAL ledger whose write lock another connection holds (readers still read)."""
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(_LEDGER_DDL)
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO llm_calls (status) VALUES ('held')")
    return db, holder


def test_a_locked_ledger_never_raises_out_of_the_writer(tmp_path, monkeypatch, capsys):
    from mini_ork.dispatch import llm_dispatch as L
    monkeypatch.setattr(L, "_LEDGER_BUSY_TIMEOUT_S", 0.05, raising=False)
    db, holder = _locked_ledger(tmp_path)
    try:
        write_llm_calls_row(str(db), "gateway", "glm53_lens", "default", "mini-ork:reviewer",
                            "reviewer", "success", 106431, 0.88, "")
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert "[llm_calls] row not written (success glm53_lens)" in capsys.readouterr().err


def test_a_successful_call_stays_rc_0_when_its_ledger_row_cannot_be_written(
        tmp_path, monkeypatch, capsys):
    """2026-10-14: a panel synth wrote APPROVE, its CLI session ended `success`,
    and the node still failed `error` with no llm_calls row — the success-path
    INSERT raised out of llm_dispatch and the caller turned that into rc=1."""
    from pathlib import Path

    from mini_ork.dispatch import llm_dispatch as L
    monkeypatch.setattr(L, "_LEDGER_BUSY_TIMEOUT_S", 0.05, raising=False)
    monkeypatch.setattr(L, "cost_circuit_open", lambda *a, **k: False)
    monkeypatch.setattr(L, "resolve_lane_model", lambda *a, **k: "glm53_lens")
    monkeypatch.setattr(L, "resolve_lane_family", lambda lane, *a, **k: lane)
    monkeypatch.setenv("MO_FUSE_ENABLED", "0")
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path))
    db, holder = _locked_ledger(tmp_path)
    monkeypatch.setenv("MINI_ORK_DB", str(db))
    out = str(tmp_path / "out.txt")

    def succeeded(model, prompt, out_file, timeout_s, max_turns, **kwargs):
        Path(out_file).write_text('{"verdict": "APPROVE"}')
        return 0

    try:
        rc = L.llm_dispatch(["--node-type", "reviewer", "--prompt-text", "x", "--out", out],
                            dispatch_fn=succeeded)
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert rc == 0
    assert "[llm_calls] row not written" in capsys.readouterr().err
