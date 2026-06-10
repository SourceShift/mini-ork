"""Tests for mini_ork.otel_export — span tree shape, ID validity, env gating."""

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mini_ork.otel_export import build_payload, export

RUN_ID = "run-1700000000-12345"


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE task_runs (
          id TEXT PRIMARY KEY, task_class TEXT, recipe TEXT, status TEXT,
          cost_usd REAL, created_at INTEGER, updated_at INTEGER, ended_at INTEGER
        );
        CREATE TABLE execution_traces (
          trace_id TEXT PRIMARY KEY, run_id TEXT, task_class TEXT,
          cost_usd REAL, duration_ms INTEGER, status TEXT, created_at TEXT
        );
        CREATE TABLE llm_calls (
          id INTEGER PRIMARY KEY, run_id TEXT, feature_name TEXT, model_id TEXT,
          input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
          duration_ms INTEGER, status TEXT, session_id TEXT, ts TEXT
        );
        """
    )
    con.execute(
        "INSERT INTO task_runs VALUES (?,?,?,?,?,?,?,?)",
        (RUN_ID, "obs_smoke", "obs-smoke", "published", 0.42, 1700000000, 1700000100, 1700000100),
    )
    con.executemany(
        "INSERT INTO execution_traces VALUES (?,?,?,?,?,?,?)",
        [
            ("tr-researcher-1700000010-1", RUN_ID, "obs_smoke", 0.30, 25000, "success", "2023-11-14T22:13:55.000Z"),
            ("tr-reviewer-1700000040-1", RUN_ID, "obs_smoke", 0.12, 9000, "failure", "2023-11-14T22:14:25.000Z"),
        ],
    )
    con.executemany(
        "INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, RUN_ID, "mini-ork:researcher", "claude-opus-4-7", 1200, 340, 0.30, 24000, "success", "sess-aaa", "2023-11-14T22:13:54.000Z"),
            (2, RUN_ID, "mini-ork:reviewer", "glm-4.7", 800, 120, 0.12, 8000, "failed", None, "2023-11-14T22:14:24.000Z"),
        ],
    )
    con.commit()
    con.close()
    return str(path)


def _spans(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def test_span_tree_shape(db):
    spans = _spans(build_payload(db, RUN_ID))
    assert len(spans) == 5  # root + 2 agents + 2 llm_calls

    root = spans[0]
    assert root["name"] == f"task_run {RUN_ID}"
    assert "parentSpanId" not in root
    assert re.fullmatch(r"[0-9a-f]{32}", root["traceId"])
    assert re.fullmatch(r"[0-9a-f]{16}", root["spanId"])

    agents = {s["name"]: s for s in spans if s["name"].startswith("agent ")}
    assert set(agents) == {"agent researcher", "agent reviewer"}
    for a in agents.values():
        assert a["parentSpanId"] == root["spanId"]
        assert a["traceId"] == root["traceId"]
    assert agents["agent reviewer"]["status"]["code"] == 2  # failure -> ERROR

    llm = {s["name"]: s for s in spans if s["name"].startswith("llm_call ")}
    assert llm["llm_call researcher"]["parentSpanId"] == agents["agent researcher"]["spanId"]
    assert llm["llm_call reviewer"]["parentSpanId"] == agents["agent reviewer"]["spanId"]


def test_llm_call_attributes(db):
    spans = _spans(build_payload(db, RUN_ID))
    call = next(s for s in spans if s["name"] == "llm_call researcher")
    attrs = {a["key"]: a["value"] for a in call["attributes"]}
    assert attrs["llm.model_name"]["stringValue"] == "claude-opus-4-7"
    assert attrs["llm.token_count.prompt"]["intValue"] == "1200"
    assert attrs["llm.token_count.completion"]["intValue"] == "340"
    assert attrs["llm.usage.total_cost_usd"]["doubleValue"] == 0.30
    assert attrs["langfuse.session.id"]["stringValue"] == "sess-aaa"
    # timestamps: end - duration == start
    assert int(call["endTimeUnixNano"]) - int(call["startTimeUnixNano"]) == 24000 * 1_000_000


def test_deterministic_ids(db):
    a = _spans(build_payload(db, RUN_ID))
    b = _spans(build_payload(db, RUN_ID))
    assert [(s["traceId"], s["spanId"]) for s in a] == [(s["traceId"], s["spanId"]) for s in b]


def test_unknown_run_exits(db):
    with pytest.raises(SystemExit):
        build_payload(db, "run-does-not-exist")


def test_live_mode_is_noop_without_creds(db, monkeypatch, capsys):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert export(RUN_ID, db, dry_run=False) == 0
    assert "no-op" in capsys.readouterr().err


def test_cli_dry_run_emits_valid_otlp_json(db):
    out = subprocess.run(
        [sys.executable, "-m", "mini_ork.otel_export", RUN_ID, "--db", db, "--dry-run"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert out.returncode == 0
    payload = json.loads(out.stdout)
    assert payload["resourceSpans"][0]["resource"]["attributes"][0]["key"] == "service.name"
    assert len(_spans(payload)) == 5
