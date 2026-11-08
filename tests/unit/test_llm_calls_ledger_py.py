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


def test_native_writer_populates_lane_column_from_model_id(tmp_path):
    """The PRAGMA-gated optional-columns loop writes ("lane", model_id), so a
    migrated ledger records the lane under its own field too. The DDLs above
    deliberately stay lane-less — they are the un-migrated fail-soft case."""
    db = tmp_path / "calls_with_lane.db"
    with sqlite3.connect(db) as con:
        con.execute("""CREATE TABLE llm_calls (
            provider TEXT, model_id TEXT, tier TEXT, feature_name TEXT, actor TEXT,
            status TEXT, duration_ms INTEGER, cost_usd REAL, error_message TEXT,
            input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
            metadata_json TEXT, iter INTEGER, run_id TEXT, traceparent TEXT,
            session_id TEXT, lane TEXT
        )""")
    write_llm_calls_row(str(db), "anthropic", "glm53_lens", "default",
                        "mini-ork:test", "researcher", "success", 1234, 0.0021, "",
                        100, 20)
    with sqlite3.connect(db) as con:
        row = con.execute("SELECT model_id, lane FROM llm_calls").fetchone()
    assert row == ("glm53_lens", "glm53_lens"), row


_LANE_DDL = """CREATE TABLE llm_calls (
    provider TEXT, model_id TEXT, tier TEXT, feature_name TEXT, actor TEXT,
    status TEXT, duration_ms INTEGER, cost_usd REAL, error_message TEXT,
    input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
    metadata_json TEXT, cached_input_tokens INTEGER,
    cache_creation_input_tokens INTEGER, cost_input_uncached_usd REAL,
    cost_input_cached_usd REAL, cost_cache_write_usd REAL,
    iter INTEGER, run_id TEXT, traceparent TEXT, session_id TEXT, lane TEXT
)"""


def _lane_of(db) -> object:
    with sqlite3.connect(db) as con:
        return con.execute("SELECT lane FROM llm_calls").fetchone()[0]


def test_persist_call_records_the_serving_lane(tmp_path):
    """``persist_call`` stores the model that produced the row as its lane."""
    from mini_ork.dispatch.telemetry import DispatchResult, persist_call

    db = tmp_path / "lane.db"
    with sqlite3.connect(db) as con:
        con.execute(_LANE_DDL)
    persist_call(db, DispatchResult(ok=True, rc=0, text="hi", model="glm-4.6",
                                    cost_usd=0.01, duration_ms=5),
                 provider="gateway", feature_name="f")
    assert _lane_of(db) == "glm-4.6"


def test_persist_call_without_a_model_writes_null_not_empty(tmp_path):
    """An unresolved lane is NULL: a per-lane roll-up must not see a "" group.

    ``DispatchResult`` defaults ``model`` to ``""``, so the empty string is the
    "nothing resolved a lane" case and has to stay distinguishable from a row
    written before the column existed.
    """
    from mini_ork.dispatch.telemetry import DispatchResult, persist_call

    db = tmp_path / "lane.db"
    with sqlite3.connect(db) as con:
        con.execute(_LANE_DDL)
    persist_call(db, DispatchResult(ok=False, rc=1, text="", model="",
                                    cost_usd=0.0, duration_ms=5),
                 provider="gateway", feature_name="f")
    assert _lane_of(db) is None


def test_persist_call_drops_lane_on_an_unmigrated_ledger(tmp_path):
    """No lane column -> the key is filtered out, the row still lands."""
    from mini_ork.dispatch.telemetry import DispatchResult, persist_call

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as con:
        con.execute(_LANE_DDL.replace(" session_id TEXT, lane TEXT", " session_id TEXT"))
    rowid = persist_call(db, DispatchResult(ok=True, rc=0, text="hi",
                                           model="glm-4.6", cost_usd=0.01,
                                           duration_ms=5),
                         provider="gateway", feature_name="f")
    assert rowid is not None
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1


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
