"""RB-32b: spend from shape-rejected fallback attempts reaches the ledger.

A fallback chain used to leave only the final result's row. `_attempt_sink`
wrote a row per dead lane but pinned its cost/tokens to 0, on the assumption
that the final row carried them; `dispatch_with_fallback` returns only the last
attempt, so a lane that returned billable output and failed the shape check
vanished from the ledger at $0. The daily budget circuit undercounted (45 rows
in one week, avg 303 s each).

These tests are hermetic: the provider transport is a per-lane stub table, the
ledger is a temp sqlite file, and run-identity env vars are stripped.
"""

import sqlite3

import pytest

import mini_ork.dispatch.llm_dispatch as llm_dispatch_mod
import mini_ork.dispatch.providers as providers
from mini_ork.dispatch.models import DispatchRequest, DispatchResult, TokenUsage


def _looks_like_json(result) -> bool:
    text = (getattr(result, "text", "") or "").strip()
    return text.startswith("{") or text.startswith("[")


def _lane_results():
    """Lane A: usage {in 1000, out 200, cost 0.01}, output fails the shape
    check. Lane B: succeeds with cost 0.02. The kickoff's exact scenario."""
    return {
        "laneA": DispatchResult(
            ok=True, rc=0, text="prose answer, definitely not json", model="laneA",
            usage=TokenUsage(input_tokens=1000, output_tokens=200),
            cost_usd=0.01, duration_ms=1000,
        ),
        "laneB": DispatchResult(
            ok=True, rc=0, text='{"plan": "ok"}', model="laneB",
            usage=TokenUsage(input_tokens=500, output_tokens=100),
            cost_usd=0.02, duration_ms=500,
        ),
    }


def _make_ledger(tmp_path) -> str:
    """Temp sqlite DB holding just the columns the row writer inserts."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(str(db))
    con.execute(
        """
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            provider TEXT, model_id TEXT, tier TEXT, feature_name TEXT,
            actor TEXT, status TEXT, duration_ms INTEGER, cost_usd REAL,
            error_message TEXT, iter INTEGER, run_id TEXT, traceparent TEXT,
            input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0, metadata_json TEXT,
            session_id TEXT, error_category TEXT, retryable INTEGER,
            cached_input_tokens INTEGER DEFAULT 0,
            cache_creation_input_tokens INTEGER DEFAULT 0,
            thinking_tokens INTEGER,
            cost_input_uncached_usd REAL, cost_input_cached_usd REAL,
            cost_cache_write_usd REAL
        )
        """
    )
    con.commit()
    con.close()
    return str(db)


def _rows(db: str):
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT model_id, cost_usd, input_tokens, output_tokens"
            " FROM llm_calls ORDER BY id"
        ).fetchall()
    finally:
        con.close()


def _install_stub_dispatcher(monkeypatch, lane_results):
    """Stub the provider transport and return a ``dispatch_fn`` for the wrapper.

    Multi-lane models walk the REAL ``dispatch_with_fallback`` with the
    wrapper's own ``on_attempt`` observer; a single-lane model dispatches
    directly. Either way the out_file/.cost/.model/.tokens sidecars are written
    exactly as the real dispatcher does."""
    def fake_dispatch_model(req, root=None):
        return lane_results[req.model]

    monkeypatch.setattr(providers, "dispatch_model", fake_dispatch_model)

    def stub(model, prompt, out_file, timeout_s=1500, max_turns=60,
             on_attempt=None, accept=None):
        lanes = [m.strip() for m in str(model).split(",") if m.strip()]
        req = DispatchRequest(model=lanes[0], prompt=prompt,
                              timeout_s=float(timeout_s))
        if len(lanes) > 1 and on_attempt is not None:
            res = providers.dispatch_with_fallback(
                req, lanes, accept=_looks_like_json, on_attempt=on_attempt)
        else:
            res = fake_dispatch_model(req)
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write(res.text)
        with open(out_file + ".cost", "w", encoding="utf-8") as fh:
            fh.write(f"{res.cost_usd:.6f}")
        with open(out_file + ".model", "w", encoding="utf-8") as fh:
            fh.write(res.model or lanes[0])
        u = res.usage
        thinking = "" if u.thinking_tokens is None else str(u.thinking_tokens)
        with open(out_file + ".tokens", "w", encoding="utf-8") as fh:
            fh.write(f"{u.input_tokens}\t{u.output_tokens}\t"
                     f"{u.cached_input_tokens}\t{u.cache_creation_tokens}\t"
                     f"{thinking}")
        if not res.ok and res.error:
            with open(out_file + ".err.log", "w", encoding="utf-8") as fh:
                fh.write(res.error.rstrip() + "\n")
        return 0 if res.ok else res.rc

    return stub


def _hermetic_env(monkeypatch, db: str) -> None:
    """Point the wrapper at the temp ledger and strip every run-identity
    variable, so nothing here can touch a live run dir or live DB."""
    for var in ("MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_HOME",
                "MINI_ORK_ROOT", "MINI_ORK_DEFAULT_MODEL", "MO_TRACEPARENT",
                "MO_RECURSIVE_ITER", "MO_LANE_ACTOR", "MO_LANE_TIER",
                "MO_NODE_ID", "MO_TARGET_CWD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MINI_ORK_DB", db)


def _run_wrapper(monkeypatch, tmp_path, model, lane_results=None):
    db = _make_ledger(tmp_path)
    _hermetic_env(monkeypatch, db)
    stub = _install_stub_dispatcher(
        monkeypatch, lane_results if lane_results is not None else _lane_results())
    out_file = tmp_path / "out.txt"
    argv = ["--node-type", "researcher", "--prompt-text", "plan",
            "--out", str(out_file), "--model", model]
    rc = llm_dispatch_mod.llm_dispatch(argv, dispatch_fn=stub)
    return rc, _rows(db)


def test_rejected_then_accepted_bills_each_attempt_once(monkeypatch, tmp_path):
    """Two rows, 0.01 and 0.02; the run total is 0.03."""
    rc, rows = _run_wrapper(monkeypatch, tmp_path, "laneA,laneB")

    assert rc == 0
    assert len(rows) == 2
    costs = sorted(row[1] for row in rows)
    assert costs[0] == pytest.approx(0.01)
    assert costs[1] == pytest.approx(0.02)
    assert sum(row[1] for row in rows) == pytest.approx(0.03)


def test_rejected_attempt_row_carries_its_real_usage(monkeypatch, tmp_path):
    """The rejected lane's row has the tokens/cost parsed from its output."""
    _rc, rows = _run_wrapper(monkeypatch, tmp_path, "laneA,laneB")

    lane_a = [row for row in rows if row[0] == "laneA"]
    assert len(lane_a) == 1
    assert lane_a[0][1] == pytest.approx(0.01)
    assert lane_a[0][1] != pytest.approx(0.0)
    assert lane_a[0][2] == 1000
    assert lane_a[0][3] == 200


def test_final_row_carries_only_the_final_attempt(monkeypatch, tmp_path):
    """No chain total on the serving row: lane B alone, not 1500/300."""
    _rc, rows = _run_wrapper(monkeypatch, tmp_path, "laneA,laneB")

    lane_b = [row for row in rows if row[0] == "laneB"]
    assert len(lane_b) == 1
    assert lane_b[0][1] == pytest.approx(0.02)
    assert lane_b[0][2] == 500
    assert lane_b[0][3] == 100


def test_all_lanes_rejected_bills_each_attempt_once(monkeypatch, tmp_path):
    """No double count when the chain collapses on shape: the last attempt is
    both an attempt row and the final failure row, but its spend lands once."""
    both_rejected = {
        "laneA": DispatchResult(
            ok=True, rc=0, text="prose answer", model="laneA",
            usage=TokenUsage(input_tokens=10, output_tokens=2),
            cost_usd=0.01, duration_ms=1000,
        ),
        "laneB": DispatchResult(
            ok=True, rc=0, text="still prose", model="laneB",
            usage=TokenUsage(input_tokens=20, output_tokens=4),
            cost_usd=0.02, duration_ms=500,
        ),
    }
    rc, rows = _run_wrapper(monkeypatch, tmp_path, "laneA,laneB", both_rejected)

    assert rc != 0
    assert len(rows) == 3  # attempt A, attempt B, final failure row
    assert sum(row[1] for row in rows) == pytest.approx(0.03)


def test_single_successful_attempt_one_row_unchanged(monkeypatch, tmp_path):
    """Regression: a lone successful lane leaves exactly its one row."""
    rc, rows = _run_wrapper(monkeypatch, tmp_path, "laneB")

    assert rc == 0
    assert len(rows) == 1
    assert rows[0][0] == "laneB"
    assert rows[0][1] == pytest.approx(0.02)
    assert rows[0][2] == 500
    assert rows[0][3] == 100
