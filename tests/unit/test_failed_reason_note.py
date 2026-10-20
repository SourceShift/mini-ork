"""RB-30 — every failed run records why in ``task_runs.notes``.

Three surfaces, each hermetic (tmp sqlite / tmp run dir, no providers):

1. ``execute``'s fail-count branch: a run with failed nodes writes a note
   naming the node ids and their finish reasons (capped at 300 chars).
2. ``run_reaper``'s retry-hint fallback: the reason the engine actually writes
   is nested at ``needs_change.detail``, not the old top-level ``detail``.
3. ``plan._mark_failed``: the bare ``planner_dispatch_rc_N`` gains the first
   plan-stage ``llm_calls.error_message`` (≤200 chars).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

import mini_ork
from mini_ork.cli import execute, plan
from mini_ork.orchestration import run_reaper

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(mini_ork.__file__)))

_TASK_RUNS_DDL = """
CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, applied_at INTEGER, checksum TEXT);
CREATE TABLE task_runs (
  id TEXT PRIMARY KEY, task_class TEXT NOT NULL, recipe TEXT,
  workflow_version TEXT NOT NULL DEFAULT 'latest', kickoff_path TEXT NOT NULL,
  plan_path TEXT, plan_hash TEXT, artifact_path TEXT, artifact_hash TEXT,
  status TEXT NOT NULL DEFAULT 'classified',
  verdict TEXT, cost_usd REAL NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0, trace_id TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, ended_at INTEGER,
  parent_epic_id TEXT, notes TEXT
);
CREATE TABLE llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL, model_id TEXT NOT NULL, tier TEXT NOT NULL,
  feature_name TEXT NOT NULL, actor TEXT,
  run_id INTEGER, iter INTEGER,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('success','failed')),
  finish_reason TEXT, error_message TEXT, traceparent TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  session_id TEXT, error_category TEXT, retryable INTEGER
);
"""

_RUN_IDENTITY_VARS = (
    "MINI_ORK_RUN_ID", "MINI_ORK_RUN_DIR", "MINI_ORK_DB", "MINI_ORK_HOME",
    "MINI_ORK_WORKFLOW", "MINI_ORK_RECIPE", "MINI_ORK_RECIPE_ROOT",
    "MINI_ORK_PLAN_PATH", "MINI_ORK_TASK_CLASS", "MINI_ORK_DRY_RUN",
    "MO_PLACEMENT",
)


def _fresh_run(tmp_path, run_id, steps):
    home = tmp_path / "home"
    run_dir = tmp_path / "run"
    home.mkdir()
    run_dir.mkdir()
    db_path = str(home / "state.db")
    con = sqlite3.connect(db_path)
    con.executescript(_TASK_RUNS_DDL)
    now = int(time.time())
    con.execute(
        "INSERT INTO task_runs (id, task_class, kickoff_path, status, "
        "created_at, updated_at, notes) VALUES (?,?,?,?,?,?,?)",
        (run_id, "framework_edit", "/tmp/kickoff.md", "executing", now, now, None))
    con.commit()
    con.close()
    plan_path = str(run_dir / "plan.json")
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump({
            "objective": "spec test run", "assumptions": [],
            "decomposition": steps, "risk_notes": [],
            "artifact_contract": {"outputs": [], "success_verifiers": []},
            "verifier_contract": {"checks": []},
        }, fh)
    return db_path, str(run_dir), plan_path


def _clean_env(monkeypatch, db_path, run_dir, plan_path, run_id):
    for var in _RUN_IDENTITY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MINI_ORK_HOME", os.path.dirname(db_path))
    monkeypatch.setenv("MINI_ORK_DB", db_path)
    monkeypatch.setenv("MINI_ORK_RUN_ID", run_id)
    monkeypatch.setenv("MINI_ORK_RUN_DIR", run_dir)
    monkeypatch.setenv("MINI_ORK_PLAN_PATH", plan_path)
    monkeypatch.setenv("MO_NODE_GIT_CKPT", "0")


def _row(db_path, run_id):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT status, notes FROM task_runs WHERE id=?", (run_id,)).fetchone()
    finally:
        con.close()


def test_execute_fail_branch_names_nodes_and_finish_reasons(tmp_path, monkeypatch):
    steps = [
        {"id": "researcher_a", "description": "node researcher_a",
         "node_type": "researcher", "depends_on": []},
        {"id": "researcher_b", "description": "node researcher_b",
         "node_type": "researcher", "depends_on": []},
    ]
    run_id = "run-unit-exec-2f"
    db_path, run_dir, plan_path = _fresh_run(tmp_path, run_id, steps)
    _clean_env(monkeypatch, db_path, run_dir, plan_path, run_id)

    calls = []

    def stub_dispatch(task_class, node_type, prompt):
        calls.append(node_type)
        return (1, "first refused") if len(calls) == 1 else (124, "timed out")

    assert execute.main([], root=PKG_ROOT, dispatch_fn=stub_dispatch) == 1
    status, notes = _row(db_path, run_id)
    assert status == "failed"
    assert notes and "failed: 2 node(s):" in notes, notes
    assert "researcher_a" in notes and "researcher_b" in notes, notes
    assert "error" in notes and "timeout" in notes, notes
    assert len(notes) <= 300, notes


def test_execute_fail_note_capped_and_keeps_head(tmp_path, monkeypatch):
    steps = [
        {"id": f"researcher_with_a_deliberately_long_identifier_{i:02d}",
         "description": f"node {i}", "node_type": "researcher", "depends_on": []}
        for i in range(12)
    ]
    run_id = "run-unit-exec-cap"
    db_path, run_dir, plan_path = _fresh_run(tmp_path, run_id, steps)
    _clean_env(monkeypatch, db_path, run_dir, plan_path, run_id)

    def stub_dispatch(task_class, node_type, prompt):
        return 1, "refused"

    assert execute.main([], root=PKG_ROOT, dispatch_fn=stub_dispatch) == 1
    _, notes = _row(db_path, run_id)
    assert notes and notes.startswith("failed:"), notes
    assert "failed: 12 node(s):" in notes, notes
    assert len(notes) <= 300, len(notes)


def test_reaper_reads_nested_needs_change_detail(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "retry-hint.json").write_text(json.dumps({
        "needs_change": {"kind": "code", "detail": "lane ran out of retries"},
        "notes": ["loses"], "detail": "top-level loses",
    }), encoding="utf-8")
    assert run_reaper._retry_hint_note(run_dir) == "lane ran out of retries"
    assert run_reaper._derive_failure_note(run_dir) == "lane ran out of retries"


def test_reaper_retry_hint_falls_back_to_notes_then_top_level(tmp_path):
    first = tmp_path / "first"
    first.mkdir()
    (first / "retry-hint.json").write_text(json.dumps({
        "needs_change": {"kind": "code"}, "notes": ["first note", "second"],
        "detail": "top-level loses",
    }), encoding="utf-8")
    assert run_reaper._retry_hint_note(first) == "first note"

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "retry-hint.json").write_text(
        json.dumps({"version": 1, "detail": "legacy detail"}), encoding="utf-8")
    assert run_reaper._retry_hint_note(legacy) == "legacy detail"


def test_mark_failed_appends_first_llm_error_after_rc(tmp_path):
    run_id = "run-unit-plan-1"
    db_path = str(tmp_path / "state.db")
    con = sqlite3.connect(db_path)
    con.executescript(_TASK_RUNS_DDL)
    now = int(time.time())
    con.execute(
        "INSERT INTO task_runs (id, task_class, kickoff_path, status, "
        "created_at, updated_at, notes) VALUES (?,?,?,?,?,?,?)",
        (run_id, "framework_edit", "/tmp/kickoff.md", "planning", now, now, None))
    con.execute(
        "INSERT INTO llm_calls (provider, model_id, tier, feature_name, actor, "
        "run_id, status, error_message) VALUES (?,?,?,?,?,?,?,?)",
        ("deepseek", "spec-lane", "default", "mini-ork:planner", "planner",
         run_id, "failed", "target cwd does not exist: /nope"))
    con.execute(
        "INSERT INTO llm_calls (provider, model_id, tier, feature_name, actor, "
        "run_id, status, error_message) VALUES (?,?,?,?,?,?,?,?)",
        ("deepseek", "spec-lane", "default", "mini-ork:planner", "planner",
         run_id, "failed", "a later failure"))
    con.commit()
    con.close()

    plan._mark_failed(db_path, run_id, "planner_dispatch_rc_127")
    status, notes = _row(db_path, run_id)
    assert status == "failed"
    assert "planner_dispatch_rc_127" in notes
    assert "target cwd does not exist: /nope" in notes
    assert "a later failure" not in notes
    assert notes.index("planner_dispatch_rc_127") < notes.index("target cwd")


def test_mark_failed_clips_message_and_no_row_leaves_rc(tmp_path):
    run_id = "run-unit-plan-clip"
    db_path = str(tmp_path / "state.db")
    con = sqlite3.connect(db_path)
    con.executescript(_TASK_RUNS_DDL)
    now = int(time.time())
    con.execute(
        "INSERT INTO task_runs (id, task_class, kickoff_path, status, "
        "created_at, updated_at, notes) VALUES (?,?,?,?,?,?,?)",
        (run_id, "framework_edit", "/tmp/kickoff.md", "planning", now, now, None))
    con.execute(
        "INSERT INTO llm_calls (provider, model_id, tier, feature_name, actor, "
        "run_id, status, error_message) VALUES (?,?,?,?,?,?,?,?)",
        ("deepseek", "spec-lane", "default", "mini-ork:planner", "planner",
         run_id, "failed", "boom:" + "a" * 400 + "TAIL"))
    con.commit()
    con.close()

    plan._mark_failed(db_path, run_id, "planner_dispatch_rc_65")
    _, notes = _row(db_path, run_id)
    assert "planner_dispatch_rc_65" in notes and "boom:" in notes
    assert "TAIL" not in notes
    assert len(notes.split("planner_dispatch_rc_65", 1)[1]) <= 202

    no_row = "run-unit-plan-norow"
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO task_runs (id, task_class, kickoff_path, status, "
        "created_at, updated_at, notes) VALUES (?,?,?,?,?,?,?)",
        (no_row, "framework_edit", "/tmp/kickoff.md", "planning", now, now, None))
    con.commit()
    con.close()
    plan._mark_failed(db_path, no_row, "planner_dispatch_rc_4")
    _, notes = _row(db_path, no_row)
    assert (notes or "").strip() == "planner_dispatch_rc_4"
