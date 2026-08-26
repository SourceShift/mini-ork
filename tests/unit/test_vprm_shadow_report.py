"""Unit tests for scripts/vprm_shadow_report.py — the read-only R1-R7 shadow report.

Covers the pure aggregation (no DB) plus window parsing and the graceful
degradation on a pre-0042 DB. The end-to-end DB path is exercised by the live
smoke; here we lock the arithmetic so a key rename in reward_vector_json can't
silently zero the report.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sqlite3
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "vprm_shadow_report",
    Path(__file__).resolve().parents[2] / "scripts" / "vprm_shadow_report.py")
assert _SPEC and _SPEC.loader
sr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sr)


def _row(source, reward_value, verdict, vector):
    return {"reward_source": source, "reward_value": reward_value,
            "reviewer_verdict": verdict, "vector": vector}


def test_summarize_empty_is_all_none_not_zero():
    rep = sr.summarize([])
    assert rep["n_eval_traces"] == 0
    # every rate must be None (no denominator), never a fake 0.0
    assert rep["coherence"]["incoherence_rate"] is None
    assert rep["process_reward"]["coverage"] is None
    assert rep["decomposed_spread"]["stats"] is None


def test_summarize_incoherence_rate_and_spread():
    rows = [
        _row("eval-exec@v1", 1.0, "pass",
             {"coherence": 1.0, "process_reward": 1.0, "decomposed": 1.0,
              "subproblem_reward": 1.0}),
        _row("eval-exec@v1", 0.5, "needs_revision",
             {"coherence": 1.0, "process_reward": 0.8, "decomposed": 0.75}),
        _row("eval-judge@v1", 1.0, "pass",
             {"coherence": 0.0, "process_reward": 0.5, "decomposed": 0.5}),
    ]
    rep = sr.summarize(rows)
    assert rep["n_eval_traces"] == 3
    assert rep["by_source"] == {"eval-exec@v1": 2, "eval-judge@v1": 1}
    # 1 of 3 coherence-scored rows is < 1.0
    assert rep["coherence"]["incoherent"] == 1
    assert rep["coherence"]["incoherence_rate"] == 1 / 3
    # |decomposed - reward_value|: 0.0, 0.25, 0.5 → max 0.5
    assert rep["decomposed_spread"]["stats"]["max"] == 0.5
    assert rep["decomposed_spread"]["n_pairs"] == 3
    # process_reward present on all 3
    assert rep["process_reward"]["coverage"] == 1.0
    # subproblem on only 1 of 3
    assert rep["subproblem_reward"]["coverage"] == 1 / 3


def test_summarize_ignores_non_numeric_and_missing_keys():
    rows = [
        _row("eval-exec@v1", None, "pass", {}),                       # no reward_value
        _row("eval-exec@v1", 0.4, "pass", {"coherence": "bad"}),      # non-numeric
        _row("eval-exec@v1", 0.4, "pass", {"decomposed": 0.4}),       # no reward_value pair? it has 0.4
    ]
    rep = sr.summarize(rows)
    # only the third row forms a decomposed pair (0.4 vs 0.4 → spread 0.0)
    assert rep["decomposed_spread"]["n_pairs"] == 1
    assert rep["coherence"]["n_scored"] == 0        # "bad" is not counted
    assert rep["coherence"]["incoherence_rate"] is None


def test_parse_window_units():
    assert sr._parse_window("7d") == dt.timedelta(days=7)
    assert sr._parse_window("24h") == dt.timedelta(hours=24)
    assert sr._parse_window("90m") == dt.timedelta(minutes=90)
    assert sr._parse_window("30s") == dt.timedelta(seconds=30)
    assert sr._parse_window("3") == dt.timedelta(days=3)   # bare → days
    assert sr._parse_window("") == dt.timedelta(days=7)    # default


def test_cutoff_iso_matches_trace_store_format():
    iso = sr._cutoff_iso(dt.timedelta(days=1))
    # same shape trace_store writes/queries: 'YYYY-MM-DDTHH:MM:SS.000Z'
    dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.000Z")


def test_has_reward_columns_false_on_legacy_schema():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE execution_traces (trace_id TEXT, run_id TEXT, "
                "reviewer_verdict TEXT, created_at TEXT)")  # pre-0042, no reward_*
    assert sr._has_reward_columns(con) is False
    con.execute("ALTER TABLE execution_traces ADD COLUMN reward_source TEXT")
    con.execute("ALTER TABLE execution_traces ADD COLUMN reward_value REAL")
    con.execute("ALTER TABLE execution_traces ADD COLUMN reward_vector_json TEXT")
    assert sr._has_reward_columns(con) is True


def test_render_text_empty_window_is_readable():
    txt = sr.render_text(sr.summarize([]), "7d")
    assert "nothing to report" in txt
