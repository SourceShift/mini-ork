"""Unit tests for ``mini_ork.mcp_context.server``.

The test file covers the stdio MCP server end-to-end:

* ``dispatch`` is driven with request dicts (no subprocess, no real
  stdio) — verifies the JSON-RPC framing, the five read-only tools,
  the per-tool error shapes (missing home, unknown tool, malformed
  input), and the control-mode gating behaviour.
* Subprocess end-to-end passes prove ``mini-ork mcp-context``
  talks JSON-RPC on real stdio in both read-only and control modes
  and stdout carries nothing but protocol lines.

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


def test_list_runs_reports_total(server_home):
    resp = _call_args({"name": "list_runs", "arguments": {}})
    body = json.loads(resp["result"]["content"][0]["text"])
    # The fixture seeds exactly two runs.
    assert body["total"] == 2
    assert len(body["runs"]) == 2


def test_list_runs_total_exceeds_page_when_more_than_limit(server_home):
    """The orchestrator answered '10 runs' from a default page of 10 — the
    ``total`` field is the count of matching runs, not the page length."""
    import sqlite3 as _sq

    db_path = server_home / "state.db"
    con = _sq.connect(str(db_path))
    try:
        now = int(time.time())
        # Add 12 more rows (status=published) so the fixture has 14 total and
        # the default page (limit=20) actually fits them all; then ask for a
        # smaller limit so total > len(rows).
        for i in range(12):
            run_id = f"run-extra-{i}"
            con.execute(
                "INSERT INTO task_runs (id, task_class, recipe, kickoff_path, "
                "status, cost_usd, created_at, updated_at) "
                "VALUES (?, 'x', 'r', 'k', 'published', 0, ?, ?)",
                (run_id, now - i, now - i),
            )
        con.commit()
    finally:
        con.close()

    resp = _call_args({"name": "list_runs", "arguments": {"limit": 5}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["total"] == 14
    assert len(body["runs"]) == 5
    assert body["total"] > len(body["runs"])


def test_list_runs_total_respects_status_filter(server_home):
    """``total`` reflects the same filter as ``runs``."""
    resp = _call_args(
        {"name": "list_runs", "arguments": {"status": "failed"}}
    )
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["total"] == 1
    assert len(body["runs"]) == 1
    assert body["runs"][0]["id"] == "run-z-1"


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


def test_subprocess_round_trip_with_control(server_home):
    """Spawn ``bin/mini-ork mcp-context --control`` and assert the 11-tool list."""
    bin_path = REPO / "bin" / "mini-ork"
    if not bin_path.exists():
        pytest.skip("bin/mini-ork missing — subprocess test needs the launcher")

    request_lines = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "start_run", "arguments": {}},
        }),
    ]) + "\n"
    proc = subprocess.run(
        [sys.executable, str(bin_path), "mcp-context", "--control"],
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
    names = sorted(t["name"] for t in lines[1]["result"]["tools"])
    expected = [
        "certify", "cost", "lanes", "learnings", "list_recipes", "list_runs",
        "run_detail", "run_status", "start_run", "stop_run", "wait_for_run",
    ]
    assert names == expected
    # `start_run` with empty args → error object (recipe required).
    body = json.loads(lines[2]["result"]["content"][0]["text"])
    assert "error" in body


def test_subprocess_control_via_env(server_home):
    """`MO_MCP_CONTROL=1` enables control tools without the CLI flag."""
    bin_path = REPO / "bin" / "mini-ork"
    if not bin_path.exists():
        pytest.skip("bin/mini-ork missing — subprocess test needs the launcher")

    request_lines = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    proc = subprocess.run(
        [sys.executable, str(bin_path), "mcp-context"],
        input=request_lines,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "MO_MCP_CONTROL": "1",
            "MINI_ORK_PROJECT_HOME": str(server_home),
            "MINI_ORK_HOME": str(server_home),
        },
    )
    assert proc.returncode == 0, (
        f"rc={proc.returncode}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    names = sorted(t["name"] for t in lines[0]["result"]["tools"])
    assert "start_run" in names
    assert "certify" in names


# ── control-mode gating (in-process) ─────────────────────────────────────


def _call_control(name: str, args: dict | None = None) -> dict:
    """Drive ``dispatch`` with the control flag enabled."""
    from mini_ork.mcp_context.server import dispatch

    return dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": name, "params": args or {}},
        control=True,
    )


def _call_args_control(args: dict | None = None) -> dict:
    return _call_control("tools/call", args or {})


def test_default_mode_rejects_control_tools(server_home):
    """Without --control, calling `start_run` returns the unknown-tool envelope."""
    resp = _call_args({"name": "start_run", "arguments": {"recipe": "x", "kickoff_markdown": "y"}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is True
    assert "unknown tool" in body["error"]


def test_control_mode_lists_eleven_tools(server_home):
    """With control=True, tools/list returns 5 + 6 = 11 names."""
    resp = _call_control("tools/list")
    names = sorted(t["name"] for t in resp["result"]["tools"])
    assert len(names) == 11
    assert names == sorted([
        "certify", "cost", "lanes", "learnings", "list_recipes", "list_runs",
        "run_detail", "run_status", "start_run", "stop_run", "wait_for_run",
    ])


# ── list_recipes ────────────────────────────────────────────────────────


def test_list_recipes_scans_engine_root(server_home, monkeypatch, tmp_path):
    """`list_recipes` reads the engine root via the control resolver."""
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    for name, desc in [("alpha", "Alpha recipe"), ("beta", "Beta recipe")]:
        d = recipes_dir / name
        d.mkdir()
        (d / "workflow.yaml").write_text("name: x\n", encoding="utf-8")
        (d / "task_class.yaml").write_text(
            f"description: {desc}\nmore: ignored\n", encoding="utf-8"
        )
    # Half-baked recipe — missing task_class.yaml — must be skipped.
    half = recipes_dir / "half"
    half.mkdir()
    (half / "workflow.yaml").write_text("name: x\n", encoding="utf-8")

    monkeypatch.setattr("mini_ork.web.control._mini_ork_root", lambda: tmp_path)

    resp = _call_args_control({"name": "list_recipes", "arguments": {}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    by_id = {r["id"]: r for r in body["recipes"]}
    # Kickoff: each entry is {id, description, source, nodes} for engine
    # recipes (no ``overrides_engine`` flag when nothing is shadowed).
    assert by_id["alpha"]["description"] == "Alpha recipe"
    assert by_id["alpha"]["source"] == "engine"
    assert by_id["alpha"]["nodes"] == 0
    assert by_id["alpha"].get("overrides_engine") is None
    assert by_id["beta"]["description"] == "Beta recipe"
    assert by_id["beta"]["source"] == "engine"
    assert "half" not in by_id


def test_list_recipes_includes_project_recipe(server_home, monkeypatch, tmp_path):
    """`list_recipes` exposes project recipes under <home>/recipes and
    marks a shadowing project entry with ``overrides_engine: true``."""
    # Engine root: an empty dir; we only care about the project entry.
    engine = tmp_path / "engine"
    engine.mkdir()
    monkeypatch.setattr("mini_ork.web.control._mini_ork_root", lambda: engine)

    # Project recipe: <home>/recipes/my-audit/
    proj = server_home / "recipes" / "my-audit"
    proj.mkdir(parents=True)
    (proj / "workflow.yaml").write_text("name: my-audit\n", encoding="utf-8")
    (proj / "task_class.yaml").write_text(
        "name: my_audit\ndescription: My audit recipe\n",
        encoding="utf-8",
    )

    resp = _call_args_control({"name": "list_recipes", "arguments": {}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    by_id = {r["id"]: r for r in body["recipes"]}
    assert "my-audit" in by_id
    entry = by_id["my-audit"]
    assert entry["source"] == "project"
    assert entry["description"] == "My audit recipe"
    # No engine recipe with the same id, so ``overrides_engine`` is absent.
    assert entry.get("overrides_engine") is None


def test_list_recipes_marks_shadowing_project_entry(server_home, monkeypatch, tmp_path):
    """A project recipe whose id collides with an engine recipe wins and
    carries ``overrides_engine: true``; the engine entry is omitted."""
    # Engine: code-fix
    engine_recipes = tmp_path / "engine" / "recipes"
    engine_recipes.mkdir(parents=True)
    code_fix = engine_recipes / "code-fix"
    code_fix.mkdir()
    (code_fix / "workflow.yaml").write_text("name: x\n", encoding="utf-8")
    (code_fix / "task_class.yaml").write_text(
        "name: code_fix\ndescription: Engine code-fix\n",
        encoding="utf-8",
    )
    # And one engine-only entry that should still surface.
    only_engine = engine_recipes / "only-engine"
    only_engine.mkdir()
    (only_engine / "workflow.yaml").write_text("name: x\n", encoding="utf-8")
    (only_engine / "task_class.yaml").write_text(
        "name: only_engine\ndescription: Engine only\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "mini_ork.web.control._mini_ork_root", lambda: tmp_path / "engine"
    )

    # Project: code-fix (shadows) + my-add (project-only)
    proj_recipes = server_home / "recipes"
    proj_code_fix = proj_recipes / "code-fix"
    proj_code_fix.mkdir(parents=True)
    (proj_code_fix / "workflow.yaml").write_text("name: x\n", encoding="utf-8")
    (proj_code_fix / "task_class.yaml").write_text(
        "name: code_fix\ndescription: Project code-fix\n",
        encoding="utf-8",
    )
    proj_add = proj_recipes / "my-add"
    proj_add.mkdir(parents=True)
    (proj_add / "workflow.yaml").write_text("name: x\n", encoding="utf-8")
    (proj_add / "task_class.yaml").write_text(
        "name: my_add\ndescription: Project add\n", encoding="utf-8"
    )

    resp = _call_args_control({"name": "list_recipes", "arguments": {}})
    body = json.loads(resp["result"]["content"][0]["text"])
    by_id = {r["id"]: r for r in body["recipes"]}
    # Single code-fix entry, source=project, overrides_engine=True.
    assert by_id["code-fix"]["source"] == "project"
    assert by_id["code-fix"]["overrides_engine"] is True
    assert by_id["code-fix"]["description"] == "Project code-fix"
    # Engine-only entry still surfaces, source=engine.
    assert by_id["only-engine"]["source"] == "engine"
    assert by_id["only-engine"].get("overrides_engine") is None
    # Project-only entry surfaces, source=project.
    assert by_id["my-add"]["source"] == "project"
    assert by_id["my-add"].get("overrides_engine") is None


# ── start_run ───────────────────────────────────────────────────────────


def test_start_run_passes_to_launch_run(server_home, monkeypatch):
    """`start_run` calls control.launch_run with recipe + kickoff + MO_TARGET_CWD."""
    # Parent shell may leak MINI_ORK_PROJECT_HOME; the kickoff clause of
    # `home.parent` only applies without the env override.
    monkeypatch.delenv("MINI_ORK_PROJECT_HOME", raising=False)
    captured: dict = {}

    def fake_launch(home, recipe, kickoff, run_id=None, extra_env=None):
        captured["home"] = home
        captured["recipe"] = recipe
        captured["kickoff"] = kickoff
        captured["extra_env"] = extra_env
        return {
            "ok": True,
            "run_id": "run-test",
            "recipe": recipe,
            "pid": 99999,
            "kickoff_path": str(home / "runs-inbox" / "run-test.md"),
            "log_path": str(home / "runs-inbox" / "run-test.launch.log"),
        }

    monkeypatch.setattr("mini_ork.web.control.launch_run", fake_launch)

    kickoff = "# hello\nbody\n"
    resp = _call_args_control({
        "name": "start_run",
        "arguments": {"recipe": "framework-edit", "kickoff_markdown": kickoff},
    })
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    assert body["ok"] is True
    assert body["run_id"] == "run-test"
    assert captured["recipe"] == "framework-edit"
    assert captured["kickoff"] == kickoff
    # MO_TARGET_CWD must point at the project root (home.parent), not the home itself.
    assert captured["extra_env"] == {"MO_TARGET_CWD": str(server_home.parent)}


def test_start_run_passes_project_home_when_set(server_home, monkeypatch):
    """MINI_ORK_PROJECT_HOME wins over home.parent for MO_TARGET_CWD."""
    captured: dict = {}

    def fake_launch(home, recipe, kickoff, run_id=None, extra_env=None):
        captured["extra_env"] = extra_env
        return {
            "ok": True,
            "run_id": "rid",
            "recipe": recipe,
            "pid": 1,
            "kickoff_path": "x",
            "log_path": "y",
        }

    monkeypatch.setattr("mini_ork.web.control.launch_run", fake_launch)
    monkeypatch.setenv("MINI_ORK_PROJECT_HOME", "/custom/project")

    resp = _call_args_control({
        "name": "start_run",
        "arguments": {"recipe": "code-fix", "kickoff_markdown": "body"},
    })
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["ok"] is True
    assert captured["extra_env"] == {"MO_TARGET_CWD": "/custom/project"}


def test_start_run_returns_error_object_on_failure(server_home, monkeypatch):
    def fake_launch(home, recipe, kickoff, run_id=None, extra_env=None):
        return {"ok": False, "error": "invalid recipe"}

    monkeypatch.setattr("mini_ork.web.control.launch_run", fake_launch)
    resp = _call_args_control({
        "name": "start_run",
        "arguments": {"recipe": "../bad", "kickoff_markdown": "x"},
    })
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is True
    assert "invalid recipe" in body["error"]


def test_start_run_requires_recipe(server_home):
    resp = _call_args_control({
        "name": "start_run",
        "arguments": {"kickoff_markdown": "x"},
    })
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is True
    assert "recipe is required" in body["error"]


# ── run_status ──────────────────────────────────────────────────────────


def test_run_status_maps_node_lifecycle(server_home):
    """node_end → done, node_start only → running, otherwise pending."""
    now = int(time.time())
    con = sqlite3.connect(str(server_home / "state.db"))
    try:
        # run-z-2 already exists with status='published'; add node events.
        con.execute(
            "INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ev-1", "run-z-2", "node_start",
             json.dumps({"node_id": "planner", "node_type": "planner"}), now - 100),
        )
        con.execute(
            "INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ev-2", "run-z-2", "node_end",
             json.dumps({"node_id": "planner", "node_type": "planner"}), now - 90),
        )
        con.execute(
            "INSERT INTO run_events(event_id, run_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ev-3", "run-z-2", "node_start",
             json.dumps({"node_id": "implementer", "node_type": "implementer"}), now - 50),
        )
        con.commit()
    finally:
        con.close()

    resp = _call_args_control({"name": "run_status", "arguments": {"run_id": "run-z-2"}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    assert body["status"] == "published"
    assert body["recipe"] == "framework-edit"
    assert isinstance(body["cost_usd"], (int, float))
    by_node = {n["node_id"]: n["state"] for n in body["nodes"]}
    assert by_node["planner"] == "done"
    assert by_node["implementer"] == "running"


def test_run_status_unknown_run_is_error(server_home):
    resp = _call_args_control({"name": "run_status", "arguments": {"run_id": "nope"}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is True
    assert "not found" in body["error"]


# ── wait_for_run ────────────────────────────────────────────────────────


def test_wait_for_run_returns_terminal_immediately(server_home, monkeypatch):
    """When the run is already terminal, return immediately with verdict + log tail."""
    monkeypatch.setenv("MO_MCP_POLL_S", "1")
    # run-z-2 is 'published' in the fixture — already terminal.
    resp = _call_args_control({
        "name": "wait_for_run",
        "arguments": {"run_id": "run-z-2", "timeout_s": 30},
    })
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    assert body["terminal"] is True
    assert body["status"] == "published"
    assert body["waited_s"] >= 0
    # verdict + launch_log_tail may be absent if the log file wasn't created,
    # but verdict.json is written by the fixture.
    if "verdict" in body:
        assert body["verdict"]["verdict"] == "APPROVE"


def test_wait_for_run_times_out_for_running_run(server_home, monkeypatch):
    """For a non-terminal run, return with terminal=False after timeout."""
    monkeypatch.setenv("MO_MCP_POLL_S", "1")
    # run-z-1 has status='failed' (terminal). Use a non-terminal fake
    # by monkeypatching _run_status to always return 'running'.
    calls = {"n": 0}

    def fake_status(home, args):
        calls["n"] += 1
        return {"status": "running", "recipe": "x", "cost_usd": 0.0, "nodes": []}

    monkeypatch.setattr("mini_ork.mcp_context.server._run_status", fake_status)
    resp = _call_args_control({
        "name": "wait_for_run",
        "arguments": {"run_id": "run-z-1", "timeout_s": 30},
    })
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    assert body["terminal"] is False
    assert body["status"] == "running"
    assert calls["n"] >= 1


# ── stop_run ────────────────────────────────────────────────────────────


def test_stop_run_soft_calls_stop_run(server_home, monkeypatch):
    captured: dict = {}

    def fake_stop(home, db, run_id):
        captured["fn"] = "stop"
        captured["run_id"] = run_id
        return {"ok": True, "action": "stop", "task_run_id": run_id}

    monkeypatch.setattr("mini_ork.web.control.stop_run", fake_stop)

    resp = _call_args_control({
        "name": "stop_run",
        "arguments": {"run_id": "run-z-1", "hard": False},
    })
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    assert body["ok"] is True
    assert captured["fn"] == "stop"


def test_stop_run_hard_calls_kill_run(server_home, monkeypatch):
    captured: dict = {}

    def fake_kill(home, db, run_id):
        captured["fn"] = "kill"
        captured["run_id"] = run_id
        return {"ok": True, "action": "kill", "task_run_id": run_id}

    monkeypatch.setattr("mini_ork.web.control.kill_run", fake_kill)

    resp = _call_args_control({
        "name": "stop_run",
        "arguments": {"run_id": "run-z-1", "hard": True},
    })
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    assert body["ok"] is True
    assert captured["fn"] == "kill"


# ── certify ─────────────────────────────────────────────────────────────


def test_certify_parses_json_output(server_home, monkeypatch):
    cert = {"ok": True, "verdict": "PASS", "issue": "min-ork-zed"}
    fake_proc = type("P", (), {
        "returncode": 0,
        "stdout": json.dumps(cert),
        "stderr": "",
    })()

    def fake_run(cmd, cwd=None, env=None, capture_output=None, text=None, timeout=None):
        captured["cmd"] = cmd
        captured["env_keys"] = sorted((env or {}).keys())
        return fake_proc

    captured: dict = {}
    monkeypatch.setattr("mini_ork.mcp_context.server.subprocess.run", fake_run)

    resp = _call_args_control({
        "name": "certify",
        "arguments": {"issue": "min-ork-zed", "base": "HEAD~1", "head": "HEAD"},
    })
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    assert body["exit_code"] == 0
    assert body["certificate"] == cert
    assert "stderr_tail" not in body
    # Command shape must match the kickoff spec.
    assert captured["cmd"][:2] == [sys.executable, str(REPO / "bin" / "mini-ork")]
    assert "certify" in captured["cmd"]
    assert "--json" in captured["cmd"]
    # Scrubbed env: NO provider keys leaked.
    for k in captured["env_keys"]:
        assert "API_KEY" not in k
        assert "ANTHROPIC_" not in k
        assert k != "MINI_ORK_SECRETS"


def test_certify_handles_non_json_output(server_home, monkeypatch):
    fake_proc = type("P", (), {
        "returncode": 2,
        "stdout": "not json at all",
        "stderr": "boom",
    })()

    def fake_run(*args, **kw):
        return fake_proc

    monkeypatch.setattr("mini_ork.mcp_context.server.subprocess.run", fake_run)

    resp = _call_args_control({
        "name": "certify",
        "arguments": {"issue": "min-ork-zed"},
    })
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    assert body["exit_code"] == 2
    assert body["certificate"] is None
    assert body["stdout_tail"] == "not json at all"
    assert body["stderr_tail"] == "boom"


def test_certify_requires_issue(server_home):
    resp = _call_args_control({"name": "certify", "arguments": {}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is True
    assert "issue is required" in body["error"]