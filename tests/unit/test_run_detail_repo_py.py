"""Unit tests for mini_ork.web.repositories.RunDetailRepository (M9 follow-up).

Seeds a tmp sqlite db with minimal rows for the run-detail tables
(task_runs / run_events / mo_events / llm_calls) and asserts each moved
query returns exactly what the inline SQL in routes/run_detail.py used to
return — plus the has_table-guarded empty behaviour for a fresh db, and
handler-level shape pins for get_events / get_llm_calls / get_dag so the
refactor stays byte-identical.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from mini_ork.web.db import StateDB  # noqa: E402
from mini_ork.web.repositories import RunDetailRepository  # noqa: E402

_SCHEMA = """
CREATE TABLE task_runs (
  id TEXT PRIMARY KEY, task_class TEXT, recipe TEXT, status TEXT,
  trace_id TEXT, created_at INTEGER, updated_at INTEGER, ended_at INTEGER,
  duration_ms INTEGER, kickoff_path TEXT, plan_path TEXT
);
CREATE TABLE run_events (
  event_id TEXT, run_id TEXT, event_type TEXT, created_at INTEGER, payload_json TEXT
);
CREATE TABLE mo_events (
  id TEXT, ts TEXT, trace_id TEXT, event_type TEXT, actor TEXT, status TEXT,
  duration_ms INTEGER, cost_usd REAL, artifact_path TEXT, payload_json TEXT
);
CREATE TABLE llm_calls (
  id INTEGER PRIMARY KEY, provider TEXT, model_id TEXT, tier TEXT,
  feature_name TEXT, actor TEXT, input_tokens INTEGER, output_tokens INTEGER,
  total_tokens INTEGER, cost_usd REAL, cached_input_tokens INTEGER,
  duration_ms INTEGER, status TEXT, finish_reason TEXT, ts TEXT, traceparent TEXT
);
"""


def _seed(db_path: Path, *, llm_cached_col: bool = True) -> None:
    con = sqlite3.connect(db_path)
    schema = _SCHEMA
    if not llm_cached_col:
        schema = schema.replace("cached_input_tokens INTEGER,", "")
    con.executescript(schema)
    con.execute(
        "INSERT INTO task_runs (id, task_class, recipe, status, trace_id, created_at,"
        " updated_at, ended_at, duration_ms, kickoff_path, plan_path)"
        " VALUES ('run-1', 'code-fix', 'code-fix', 'running', 'tr-1', 1000, 1500, NULL,"
        " NULL, '/tmp/kickoff.md', '/tmp/plan.json')"
    )
    con.execute(
        "INSERT INTO task_runs (id, task_class, recipe, status, trace_id, created_at)"
        " VALUES ('run-done', 'code-fix', NULL, 'published', NULL, 900)"
    )
    con.executemany(
        "INSERT INTO run_events (event_id, run_id, event_type, created_at, payload_json)"
        " VALUES (?,?,?,?,?)",
        [
            ("ev-1", "run-1", "node_start", 1100,
             json.dumps({"node_id": "implementer"})),
            ("ev-2", "run-1", "node_end", 1400,
             json.dumps({"node_id": "implementer", "verdict": "APPROVE",
                         "duration_ms": 12, "artifact_path": "impl-implementer.log"})),
            ("ev-3", "run-1", "node_start", 1450,
             json.dumps({"node_id": "verifier"})),
            ("ev-4", "run-1", "node_end", 1600,
             json.dumps({"node_id": "verifier", "verdict": "REQUEST_CHANGES"})),
            ("ev-5", "run-1", "emit", 1610, json.dumps({"note": "ignored by lifecycle"})),
            ("ev-x", "run-0", "node_start", 100, json.dumps({"node_id": "other"})),
        ],
    )
    con.executemany(
        "INSERT INTO mo_events (id, ts, trace_id, event_type, actor, status,"
        " duration_ms, cost_usd, artifact_path, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            # strict trace_id hit, inside the window (epoch 1000..now)
            ("mo-1", "2026-06-01T00:00:10.000Z", "tr-1", "node_start", "impl",
             "ok", 5, 0.01, None, "{}"),
            # different trace_id — only reachable via the time window
            ("mo-2", "2026-06-01T00:00:20.000Z", "tr-other", "emit", "impl",
             "ok", 6, 0.02, None, "{}"),
            # ancient (epoch 100 < created_at=1000) — outside the window
            ("mo-old", "1970-01-01T00:01:40.000Z", "tr-1", "emit", "impl",
             "ok", 1, 0.0, None, "{}"),
        ],
    )
    cached = 10 if llm_cached_col else None
    rows = [
        (1, "kimi", "k1", "t1", "run", "impl", 100, 50, 150, 0.01,
         5, "ok", "stop", "2026-06-01T00:00:10.000Z", "trace tr-1 abc"),
        (2, "glm", "g1", "t2", "run", "verify", 200, 60, 260, 0.02,
         6, "ok", "stop", "2026-06-01T00:00:20.000Z", "trace tr-other xyz"),
        (3, "opus", "o1", "t3", "run", "impl", 300, 70, 370, 0.03,
         7, "ok", "stop", "1970-01-01T00:01:40.000Z", "trace tr-1 abc"),
    ]
    if llm_cached_col:
        con.executemany(
            "INSERT INTO llm_calls (id, provider, model_id, tier, feature_name, actor,"
            " input_tokens, output_tokens, total_tokens, cost_usd, cached_input_tokens,"
            " duration_ms, status, finish_reason, ts, traceparent)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [r[:10] + (cached,) + r[10:] for r in rows],
        )
    else:
        con.executemany(
            "INSERT INTO llm_calls (id, provider, model_id, tier, feature_name, actor,"
            " input_tokens, output_tokens, total_tokens, cost_usd,"
            " duration_ms, status, finish_reason, ts, traceparent)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    con.commit()
    con.close()


@pytest.fixture
def seeded(tmp_path: Path) -> RunDetailRepository:
    db_path = tmp_path / "state.db"
    _seed(db_path)
    return RunDetailRepository(StateDB(db_path))


@pytest.fixture
def empty(tmp_path: Path) -> RunDetailRepository:
    """task_runs only — run_events/mo_events/llm_calls absent (has_table guards)."""
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE task_runs (id TEXT PRIMARY KEY, recipe TEXT, trace_id TEXT,"
        " created_at INTEGER, ended_at INTEGER)"
    )
    con.commit()
    con.close()
    return RunDetailRepository(StateDB(db_path))


# ── task_runs ─────────────────────────────────────────────────────────────


def test_fetch_task_run_row(seeded: RunDetailRepository) -> None:
    tr = seeded.fetch_task_run_row("run-1")
    assert tr is not None
    assert tr["id"] == "run-1"
    assert tr["kickoff_path"] == "/tmp/kickoff.md"  # SELECT * — full row
    assert seeded.fetch_task_run_row("nope") is None


def test_fetch_input_paths(seeded: RunDetailRepository) -> None:
    tr = seeded.fetch_input_paths("run-1")
    assert tr == {"kickoff_path": "/tmp/kickoff.md", "plan_path": "/tmp/plan.json",
                  "recipe": "code-fix"}
    assert seeded.fetch_input_paths("nope") is None


def test_fetch_correlation_row(seeded: RunDetailRepository) -> None:
    tr = seeded.fetch_correlation_row("run-1")
    assert tr is not None
    assert set(tr) == {"id", "trace_id", "created_at", "ended_at", "kickoff_path"}
    assert tr["trace_id"] == "tr-1"


def test_fetch_trace_window(seeded: RunDetailRepository) -> None:
    tr = seeded.fetch_trace_window("run-1")
    assert tr == {"trace_id": "tr-1", "created_at": 1000, "ended_at": None}


def test_fetch_run_recipe(seeded: RunDetailRepository) -> None:
    assert seeded.fetch_run_recipe("run-1") == {"recipe": "code-fix"}
    assert seeded.fetch_run_recipe("run-done") == {"recipe": None}
    assert seeded.fetch_run_recipe("nope") is None


# ── run_events ────────────────────────────────────────────────────────────


def test_fetch_last_run_event_ts(seeded: RunDetailRepository) -> None:
    assert seeded.fetch_last_run_event_ts("run-1") == 1610
    assert seeded.fetch_last_run_event_ts("run-done") is None


def test_fetch_run_events_mo_layout(seeded: RunDetailRepository) -> None:
    rows = seeded.fetch_run_events("run-1", 500)
    assert [r["id"] for r in rows] == ["ev-1", "ev-2", "ev-3", "ev-4", "ev-5"]
    assert rows[0]["actor"] is None and rows[0]["cost_usd"] is None
    assert rows[0]["ts"] == 1100
    limited = seeded.fetch_run_events("run-1", 2)
    assert [r["id"] for r in limited] == ["ev-1", "ev-2"]


def test_fetch_node_lifecycle_events(seeded: RunDetailRepository) -> None:
    rows = seeded.fetch_node_lifecycle_events("run-1")
    # the 'emit' event and other runs' events are filtered out by the SQL
    assert [r["event_type"] for r in rows] == [
        "node_start", "node_end", "node_start", "node_end"
    ]


# ── mo_events ─────────────────────────────────────────────────────────────


def test_fetch_mo_events_by_trace_id(seeded: RunDetailRepository) -> None:
    rows = seeded.fetch_mo_events_by_trace_id("tr-1", 500)
    assert [r["id"] for r in rows] == ["mo-old", "mo-1"]  # ts ASC
    assert seeded.fetch_mo_events_by_trace_id("tr-none", 500) == []


def test_fetch_mo_events_in_window(seeded: RunDetailRepository) -> None:
    rows = seeded.fetch_mo_events_in_window(1000, 9999999999, 500)
    assert {r["id"] for r in rows} == {"mo-1", "mo-2"}  # mo-old (epoch 100) excluded
    assert seeded.fetch_mo_events_in_window(0, 50, 500) == []


# ── llm_calls ─────────────────────────────────────────────────────────────


def test_fetch_llm_calls_by_trace_id(seeded: RunDetailRepository) -> None:
    rows = seeded.fetch_llm_calls_by_trace_id("tr-1")
    assert [r["id"] for r in rows] == [3, 1]  # ts ASC
    assert rows[0]["cached_input_tokens"] == 10  # real column when present


def test_fetch_llm_calls_in_window(seeded: RunDetailRepository) -> None:
    rows = seeded.fetch_llm_calls_in_window(1000, 9999999999)
    assert {r["id"] for r in rows} == {1, 2}


def test_llm_calls_without_cached_column(tmp_path: Path) -> None:
    """Older state.db lacks cached_input_tokens — the PRAGMA shim yields 0."""
    db_path = tmp_path / "state.db"
    _seed(db_path, llm_cached_col=False)
    repo = RunDetailRepository(StateDB(db_path))
    rows = repo.fetch_llm_calls_by_trace_id("tr-1")
    assert [r["id"] for r in rows] == [3, 1]
    assert all(r["cached_input_tokens"] == 0 for r in rows)


# ── empty db: guards return empty, never error ────────────────────────────


def test_empty_db_returns_empty_not_error(empty: RunDetailRepository) -> None:
    assert not empty.has_table("run_events")
    assert empty.fetch_task_run_row("run-1") is None
    assert empty.fetch_trace_window("run-1") is None
    assert empty.fetch_last_run_event_ts("run-1") is None
    assert empty.fetch_run_events("run-1", 500) == []
    assert empty.fetch_node_lifecycle_events("run-1") == []
    assert empty.fetch_mo_events_by_trace_id("tr-1", 500) == []
    assert empty.fetch_mo_events_in_window(0, 100, 500) == []
    assert empty.fetch_llm_calls_by_trace_id("tr-1") == []
    assert empty.fetch_llm_calls_in_window(0, 100) == []


# ── handler-level shape pins ──────────────────────────────────────────────


def test_get_events_bridge_classification_preserved(tmp_path: Path) -> None:
    from mini_ork.web.routes.run_detail import get_events

    db_path = tmp_path / "state.db"
    _seed(db_path)
    out = get_events(task_run_id="run-1", db=StateDB(db_path), limit=500)
    by_id = {e["id"]: e for e in out}
    assert by_id["mo-1"]["bridge"] == "trace_id"
    assert by_id["mo-1"]["source"] == "mo_events"
    # mo-old shares trace_id tr-1 → also matched strictly (dedup by id)
    assert by_id["mo-old"]["bridge"] == "trace_id"
    assert by_id["mo-2"]["bridge"] == "time-window"
    assert by_id["ev-1"]["source"] == "run_events"
    assert by_id["ev-1"]["bridge"] == "run_id"
    # sorted by ts as strings, exactly as the handler did
    assert out == sorted(out, key=lambda e: str(e.get("ts") or ""))


def test_get_llm_calls_bridge_classification_preserved(tmp_path: Path) -> None:
    from mini_ork.web.routes.run_detail import get_llm_calls

    db_path = tmp_path / "state.db"
    _seed(db_path)
    out = get_llm_calls(task_run_id="run-1", db=StateDB(db_path))
    by_id = {c["id"]: c for c in out}
    assert by_id[1]["bridge"] == "trace_id"
    assert by_id[3]["bridge"] == "trace_id"
    assert by_id[2]["bridge"] == "time-window"  # different traceparent, window only
    assert by_id[1]["cached_input_tokens"] == 10


def test_get_llm_calls_empty_when_table_missing(tmp_path: Path) -> None:
    from mini_ork.web.routes.run_detail import get_llm_calls

    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE task_runs (id TEXT PRIMARY KEY, trace_id TEXT,"
        " created_at INTEGER, ended_at INTEGER)"
    )
    con.execute("INSERT INTO task_runs VALUES ('run-1', 'tr-1', 100, NULL)")
    con.commit()
    con.close()
    assert get_llm_calls(task_run_id="run-1", db=StateDB(db_path)) == []
    assert get_llm_calls(task_run_id="nope", db=StateDB(db_path)) == []


def test_node_status_map_classification_preserved(tmp_path: Path) -> None:
    from mini_ork.web.routes.run_detail import _node_status_map

    db_path = tmp_path / "state.db"
    _seed(db_path)
    out = _node_status_map(StateDB(db_path), "run-1")
    assert out["implementer"] == {
        "status": "done",
        "started_at": 1100,
        "ended_at": 1400,
        "duration_ms": 12,
        "verdict": "APPROVE",
        "artifact_path": "impl-implementer.log",
    }
    assert out["verifier"]["status"] == "failed"  # REQUEST_CHANGES
    assert out["verifier"]["verdict"] == "REQUEST_CHANGES"
    assert "other" not in out  # different run
