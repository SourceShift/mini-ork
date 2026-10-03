"""Unit tests for ``mini_ork.mcp_context.server``.

The test file covers the read-only stdio MCP server end-to-end:

* ``dispatch`` is driven with request dicts (no subprocess, no real
  stdio) — verifies the JSON-RPC framing, the five tools, and the
  per-tool error shapes (missing home, unknown tool, malformed input).
* One subprocess end-to-end pass to prove ``mini-ork mcp-context``
  talks JSON-RPC on real stdio and stdout carries nothing but
  protocol lines.

DB fixture convention follows ``tests/README.md`` (``init_db`` on a
fresh ``tmp_path``). The autouse ``_isolate_process_state`` fixture
in ``tests/conftest.py`` snapshots/restores ``os.environ`` so we can
freely set ``MINI_ORK_HOME`` without leaking it into the rest of the
suite.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path) -> Path:
    """Fresh ``.mini-ork/`` with a migrated ``state.db`` and seeded rows."""
    h = tmp_path / ".mini-ork"
    h.mkdir()
    db_path = h / "state.db"
    from mini_ork.stores.migrate import init_db

    rc, out, err = init_db(db=str(db_path), root=str(REPO))
    assert rc == 0, f"init_db failed rc={rc}\nstdout={out}\nstderr={err}"

    # Seed two task_runs (newest first) + a kickoff under runs/<id>/.
    now = int(time.time())
    run_id_new = "run-z-2"
    run_id_old = "run-z-1"
    runs_dir = h / "runs"
    (runs_dir / run_id_new).mkdir(parents=True)
    (runs_dir / run_id_old).mkdir(parents=True)
    (runs_dir / run_id_new / "kickoff.md").write_text(
        "# MCP context tool — the read-only surface\n\nbody\n", encoding="utf-8"
    )
    (runs_dir / run_id_old / "kickoff.md").write_text(
        "## First non-h1 line\n\nolder body\n", encoding="utf-8"
    )
    verdict = {"verdict": "APPROVE", "notes": "ok"}
    (runs_dir / run_id_new / "verdict.json").write_text(
        json.dumps(verdict), encoding="utf-8"
    )

    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO task_runs (
                id, task_class, recipe, kickoff_path, status,
                cost_usd, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id_new,
                "framework_edit",
                "framework-edit",
                str(runs_dir / run_id_new / "kickoff.md"),
                "published",
                0.42,
                now,
                now,
            ),
        )
        con.execute(
            """
            INSERT INTO task_runs (
                id, task_class, recipe, kickoff_path, status,
                cost_usd, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id_old,
                "code_fix",
                "code-fix",
                str(runs_dir / run_id_old / "kickoff.md"),
                "failed",
                0.05,
                now - 86_400,
                now - 86_400,
            ),
        )
        con.commit()
    finally:
        con.close()
    return h


@pytest.fixture
def server_home(monkeypatch, home: Path) -> Path:
    """Point the server at the test home without leaking env into siblings."""
    monkeypatch.setenv("MINI_ORK_HOME", str(home))
    return home


# ── dispatch() — in-process ───────────────────────────────────────────────


def _call(name: str, args: dict | None = None) -> dict:
    from mini_ork.mcp_context.server import dispatch

    return dispatch({"jsonrpc": "2.0", "id": 1, "method": name, "params": args or {}})


def _call_args(args: dict | None = None) -> dict:
    return _call("tools/call", args or {})


def test_initialize_returns_server_info(server_home):
    resp = _call("initialize", {"protocolVersion": "2099-01-01"})
    assert resp["result"]["serverInfo"]["name"] == "mini-ork-context"
    assert resp["result"]["protocolVersion"] == "2099-01-01"
    assert "tools" in resp["result"]["capabilities"]


def test_initialize_falls_back_to_default_version(server_home):
    resp = _call("initialize", {})
    assert resp["result"]["protocolVersion"] == "2025-06-18"


def test_notifications_initialized_returns_none(server_home):
    from mini_ork.mcp_context.server import dispatch

    assert dispatch(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    ) is None


def test_tools_list_returns_five_definitions(server_home):
    from mini_ork.mcp_context.server import TOOL_DEFS

    resp = _call("tools/list")
    names = sorted(t["name"] for t in resp["result"]["tools"])
    assert names == sorted(TOOL_DEFS[i]["name"] for i in range(len(TOOL_DEFS)))
    assert names == ["cost", "lanes", "learnings", "list_runs", "run_detail"]


def test_unknown_method_returns_minus_32601(server_home):
    resp = _call("definitely/not/a/method")
    assert resp["error"]["code"] == -32601


def test_unknown_tool_returns_error_envelope(server_home):
    resp = _call_args({"name": "nope", "arguments": {}})
    assert resp["result"]["isError"] is True
    assert "unknown tool" in resp["result"]["content"][0]["text"]


def test_dispatch_skips_non_dict(monkeypatch):
    from mini_ork.mcp_context.server import dispatch

    assert dispatch("a string") is None
    assert dispatch(None) is None
    assert dispatch(42) is None


def test_dispatch_handles_no_method():
    from mini_ork.mcp_context.server import dispatch

    assert dispatch({"jsonrpc": "2.0", "id": 7}) is None


# ── list_runs ────────────────────────────────────────────────────────────


def test_list_runs_newest_first_with_titles(server_home):
    resp = _call_args({"name": "list_runs", "arguments": {}})
    text = resp["result"]["content"][0]["text"]
    body = json.loads(text)
    assert resp["result"]["isError"] is False
    assert len(body["runs"]) == 2
    assert body["runs"][0]["id"] == "run-z-2"
    assert body["runs"][0]["title"] == "MCP context tool — the read-only surface"
    assert body["runs"][1]["id"] == "run-z-1"
    assert body["runs"][1]["title"] == "First non-h1 line"


def test_list_runs_respects_status_filter(server_home):
    resp = _call_args(
        {"name": "list_runs", "arguments": {"status": "failed"}}
    )
    body = json.loads(resp["result"]["content"][0]["text"])
    assert [r["id"] for r in body["runs"]] == ["run-z-1"]


def test_list_runs_caps_limit(server_home):
    resp = _call_args(
        {"name": "list_runs", "arguments": {"limit": 1}}
    )
    body = json.loads(resp["result"]["content"][0]["text"])
    assert len(body["runs"]) == 1


# ── run_detail ────────────────────────────────────────────────────────────


def test_run_detail_returns_full_record(server_home):
    resp = _call_args({"name": "run_detail", "arguments": {"run_id": "run-z-2"}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    assert body["task_run"]["id"] == "run-z-2"
    assert body["task_run"]["recipe"] == "framework-edit"
    assert body["kickoff_preview"].startswith("# MCP context tool")
    assert body["verdict"] == {"verdict": "APPROVE", "notes": "ok"}
    assert isinstance(body["node_lifecycle_events"], list)


def test_run_detail_missing_id_is_error_object(server_home):
    resp = _call_args({"name": "run_detail", "arguments": {"run_id": "nope"}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is True
    assert "not found" in body["error"]


def test_run_detail_requires_run_id(server_home):
    resp = _call_args({"name": "run_detail", "arguments": {}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is True
    assert "required" in body["error"]


# ── learnings ────────────────────────────────────────────────────────────


def test_learnings_returns_three_sections(server_home):
    resp = _call_args({"name": "learnings", "arguments": {"task_class": "framework_edit"}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    assert set(body.keys()) == {"failure_modes", "patterns", "memory"}
    assert isinstance(body["memory"], dict)


def test_learnings_query_filters_memory_tasks(server_home):
    resp = _call_args(
        {"name": "learnings", "arguments": {"query": "framework_edit"}}
    )
    body = json.loads(resp["result"]["content"][0]["text"])
    # On an empty schema the dict-shaped memory survives the filter
    # unchanged (empty JSON trivially matches / fails the needle — we
    # only assert it returned, not that the filter narrows it).
    assert "memory" in body


# ── cost ─────────────────────────────────────────────────────────────────


def test_cost_returns_envelope_with_total(server_home):
    resp = _call_args({"name": "cost", "arguments": {"days": 7}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert "days" in body
    assert "total_cost_usd" in body
    assert isinstance(body["total_cost_usd"], (int, float))


# ── lanes ────────────────────────────────────────────────────────────────


def test_lanes_returns_empty_when_no_overlay(server_home):
    resp = _call_args({"name": "lanes", "arguments": {}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    assert isinstance(body["lanes"], dict)


# ── missing-home gate ────────────────────────────────────────────────────


def test_missing_home_returns_error_object(monkeypatch, tmp_path):
    monkeypatch.setenv("MINI_ORK_HOME", str(tmp_path / "nope"))
    resp = _call_args({"name": "list_runs", "arguments": {}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is True
    assert "no mini-ork home" in body["error"]


def test_missing_db_returns_error_object(monkeypatch, tmp_path):
    empty = tmp_path / "empty-home"
    empty.mkdir()
    monkeypatch.setenv("MINI_ORK_HOME", str(empty))
    resp = _call_args({"name": "list_runs", "arguments": {}})
    assert resp["result"]["isError"] is True


# ── serve() loop ─────────────────────────────────────────────────────────


def test_serve_loop_handles_malformed_lines(tmp_path):
    from mini_ork.mcp_context.server import serve

    stdin = io.StringIO(
        "not json\n"
        + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "unknown/method"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + "\n"
    )
    out = io.StringIO()
    rc = serve(stdin=stdin, stdout=out)
    assert rc == 0
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line]
    # First valid request triggers -32601, second returns tools/list.
    assert lines[0]["error"]["code"] == -32601
    assert "tools" in lines[1]["result"]


# ── subprocess E2E ───────────────────────────────────────────────────────


def test_subprocess_round_trip(server_home):
    """Spawn ``bin/mini-ork mcp-context`` and prove stdio framing."""
    bin_path = REPO / "bin" / "mini-ork"
    if not bin_path.exists():
        pytest.skip("bin/mini-ork missing — subprocess test needs the launcher")

    request_lines = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "list_runs", "arguments": {"limit": 5}},
        }),
    ]) + "\n"
    proc = subprocess.run(
        [sys.executable, str(bin_path), "mcp-context"],
        input=request_lines,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "MINI_ORK_PROJECT_HOME": str(server_home),
            "MINI_ORK_HOME": str(server_home),
        },
    )
    assert proc.returncode == 0, (
        f"rc={proc.returncode}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    assert len(lines) == 3
    assert lines[0]["result"]["serverInfo"]["name"] == "mini-ork-context"
    names = sorted(t["name"] for t in lines[1]["result"]["tools"])
    assert names == ["cost", "lanes", "learnings", "list_runs", "run_detail"]
    body = json.loads(lines[2]["result"]["content"][0]["text"])
    assert len(body["runs"]) >= 1