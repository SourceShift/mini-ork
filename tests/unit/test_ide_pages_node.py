"""``mini-ork board node <run_id> <node_id>`` — one DAG node's detail panel.

Mirrors the test_ide_pages_run fixture pattern (temp home, init_db, workflow).
Each test seeds one run directory with a small session transcript covering the
kinds the IDE draws (user, think, text, tool, todo, note) and the error /
Edit-diff colouring the kickoff names.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mini_ork.cli import board_cmd
from mini_ork.ide_pages import build_page
from mini_ork.ide_pages.node import build_node
from mini_ork.stores import migrate as mig

REPO = Path(__file__).resolve().parents[2]
RUN = "run-1791000000-abc123"
SESSION_UUID = "11111111-2222-3333-4444-555555555555"
AGENT_NODE = "implementer"
SHELL_NODE = "verifier_node"
T0 = 1_791_000_000

WORKFLOW = """\
version: 1
task_class: demo
nodes:
  - {name: planner, type: planner, model_lane: planner, prompt_ref: prompts/planner.md}
  - {name: implementer, type: implementer, model_lane: worker, prompt_ref: prompts/implementer.md,
     gates: [scope_gate, budget_gate]}
  - {name: verifier_node, type: verifier, verifier_ref: verifiers/test.py}
"""


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    recipe = h / "recipes" / "demo-recipe"
    recipe.mkdir(parents=True)
    (recipe / "workflow.yaml").write_text(WORKFLOW)
    (recipe / "task_class.yaml").write_text("name: demo\ndescription: a demo recipe\n")
    return h


def _seed(home: Path, *, status: str = "executing") -> Path:
    """One run with one agent-node session transcript + one shell-node log."""
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True)
    kickoff = home / "kickoffs" / "demo.md"
    kickoff.parent.mkdir(parents=True)
    kickoff.write_text("# Make the demo pass\n\nDetails.\n")
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO task_runs (id, recipe, status, cost_usd, created_at, updated_at, ended_at, "
        "task_class, kickoff_path, workflow_version, trace_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (RUN, "demo-recipe", status, 0.31, T0, T0 + 120, T0 + 100, "demo", str(kickoff), "latest", "tr-demo-1"))

    events = [("node_start", "implementer", "implementer", "worker", T0 + 10, None),
              ("node_end", "implementer", "implementer", "worker", T0 + 60, "done"),
              ("node_start", "verifier_node", "verifier", "verifier", T0 + 60, None)]
    for i, (kind, node, ntype, lane, ts, fin) in enumerate(events):
        payload = {"node_id": node, "node_type": ntype, "model_lane": lane}
        if fin:
            payload["finish_reason"] = fin
        con.execute("INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
                    "VALUES (?,?,?,?,?)", (f"ev-{i}", RUN, kind, json.dumps(payload), ts))
    con.execute("INSERT INTO llm_calls (provider, model_id, tier, feature_name, actor, run_id, "
                "cost_usd, status, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                ("gateway", "minimax", "default", "mini-ork:worker", "worker", RUN, 0.07, "success",
                 _iso(T0 + 50)))
    con.commit()
    con.close()

    # Session transcript covering every kind the kickoff names:
    # user (prompt), think, tool (Read), tool (Edit + diff), tool (Bash error),
    # todo (TodoWrite), text (final answer), result (note).
    session_path = run_dir / "sessions" / f"{SESSION_UUID}.jsonl"
    session_path.parent.mkdir(parents=True)
    session_path.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": "implement the fix"}
        ]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "Let me read the file first."}
        ]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Read",
             "input": {"file_path": "/tmp/x.py"}}
        ]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": [
                {"type": "text", "text": "def foo(): pass"}
            ], "is_error": False}
        ]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_2", "name": "Edit",
             "input": {"file_path": "/tmp/x.py",
                       "old_string": "def foo(): pass",
                       "new_string": "def foo(): return 42"}}
        ]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": [
                {"type": "text", "text": "applied"}
            ], "is_error": False}
        ]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_3", "name": "Bash",
             "input": {"command": "pytest"}}
        ]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_3", "content": [
                {"type": "text", "text": "ERROR: 1 failed"}
            ], "is_error": True}
        ]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_4", "name": "TodoWrite",
             "input": {"todos": [
                {"content": "read x.py", "status": "completed"},
                {"content": "edit x.py", "status": "in_progress"},
             ]}}
        ]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "I edited the file and ran pytest."}
        ]}}),
        json.dumps({"type": "result", "result": "done", "session_id": SESSION_UUID,
                    "total_cost_usd": 0.31, "num_turns": 12}),
    ]) + "\n")
    # live sidecar's result event names the session
    (run_dir / f"agent-{AGENT_NODE}.live.jsonl").write_text(
        json.dumps({"seq": 0, "stream": "stdout", "t": T0 + 60,
                    "line": json.dumps({"session_id": SESSION_UUID,
                                        "total_cost_usd": 0.31, "num_turns": 12})}) + "\n")

    # Shell-node verifier log
    (run_dir / f"verifier_{SHELL_NODE}.log").write_text(
        "[verifier] running\n[ok] verifier pass\n")

    # Operator-steering row for the run
    con = sqlite3.connect(home / "state.db")
    now_ms = int(time.time() * 1000)
    con.execute(
        "INSERT INTO operator_steering (run_id, role_target, severity, message, source, "
        "confidence, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (RUN, "implementer", "info", "be careful with the Edit tool", "ide",
         0.8, now_ms, now_ms + 3600_000))
    con.commit()
    con.close()
    return run_dir


def test_every_view_builds_ok_true_for_the_agent_node(home: Path) -> None:
    _seed(home)
    for view in ("stream", "output", "prompt", "telemetry", "learning"):
        out = build_node(home, RUN, AGENT_NODE, view=view)
        assert out["ok"] is True, view
        assert out["run"] == RUN
        assert out["node"] == AGENT_NODE
        assert out["view"] == view
        assert "kv" in out and "acts" in out


def test_every_view_builds_ok_true_for_the_shell_node(home: Path) -> None:
    _seed(home)
    for view in ("stream", "output", "prompt", "telemetry"):
        out = build_node(home, RUN, SHELL_NODE, view=view)
        assert out["ok"] is True, view
        # No LLM calls → telemetry view returns the "Deterministic node" line.
        if view == "telemetry":
            assert "Deterministic" in out["block"][0]["t"]


def test_stream_view_kinds_and_order_match_the_transcript(home: Path) -> None:
    _seed(home)
    out = build_node(home, RUN, AGENT_NODE, view="stream")
    kinds = [e["k"] for e in out["entries"]]
    # user → think → tool(Read) → tool(Edit) → tool(Bash) → todo → text → note → steer
    assert kinds == ["user", "think", "tool", "tool", "tool", "todo", "text", "note", "steer"]
    assert out["status"] == "finished"
    assert out["status_c"] == "sub"


def test_stream_view_edit_diff_lines_have_old_and_new_with_bg(home: Path) -> None:
    _seed(home)
    out = build_node(home, RUN, AGENT_NODE, view="stream")
    edit = next(e for e in out["entries"] if e["k"] == "tool" and e["head"] == "Edit")
    diffs = [ln for ln in edit["lines"] if ln.get("bg")]
    assert any(ln["bg"] == "red-bg" and ln["t"].startswith("- ") for ln in diffs)
    assert any(ln["bg"] == "green-bg" and ln["t"].startswith("+ ") for ln in diffs)


def test_stream_view_bash_error_is_red(home: Path) -> None:
    _seed(home)
    out = build_node(home, RUN, AGENT_NODE, view="stream")
    bash = next(e for e in out["entries"] if e["k"] == "tool" and e["head"] == "Bash")
    assert bash["lines"][0]["c"] == "red"


def test_stream_view_todo_render_marks_checkboxes(home: Path) -> None:
    _seed(home)
    out = build_node(home, RUN, AGENT_NODE, view="stream")
    todo = next(e for e in out["entries"] if e["k"] == "todo")
    marks = [ln["t"][0] for ln in todo["lines"]]
    assert "☒" in marks and "☐" in marks


def test_stream_view_offset_returns_only_later_entries(home: Path) -> None:
    _seed(home)
    full = build_node(home, RUN, AGENT_NODE, view="stream")
    n = len(full["entries"])
    skipped = build_node(home, RUN, AGENT_NODE, view="stream", offset=4)
    assert len(skipped["entries"]) == n - 4
    assert skipped["offset"] == n


def test_stream_view_steer_entry_shows_severity_and_message(home: Path) -> None:
    _seed(home)
    out = build_node(home, RUN, AGENT_NODE, view="stream")
    steer = next(e for e in out["entries"] if e["k"] == "steer")
    assert "info" in steer["arg"] and "be careful" in steer["arg"]
    assert steer["head"].startswith("You · steering")


def test_unknown_node_returns_ok_false(home: Path) -> None:
    _seed(home)
    out = build_node(home, RUN, "no-such-node")
    assert out["ok"] is False
    assert "no node no-such-node" in out["error"]


def test_unknown_run_returns_ok_false(home: Path) -> None:
    _seed(home)
    out = build_node(home, "run-nope", AGENT_NODE)
    assert out["ok"] is False
    assert "no run run-nope" in out["error"]


def test_path_traversal_in_run_id_returns_ok_false(home: Path) -> None:
    _seed(home)
    assert build_node(home, "../etc", AGENT_NODE)["ok"] is False
    assert build_node(home, RUN, "../etc")["ok"] is False


def test_run_dag_carries_role_dur_gates_and_heads(home: Path) -> None:
    _seed(home)
    page = build_page(home, "run", "dag", {"run": RUN})
    assert page["ok"] is True
    dag = next(s for s in page["sections"] if s["type"] == "dag")
    nodes = {n["id"]: n for c in dag["cols"] for n in c}
    assert nodes["implementer"]["role"] == "worker"
    assert nodes["implementer"]["gates"] == "scope_gate, budget_gate"
    assert nodes["implementer"]["dur"].endswith("s")
    # stage heads: one per column, in order.
    assert len(dag["heads"]) == len(dag["cols"])
    assert dag["run_title"] == "" or isinstance(dag["run_title"], str)
    assert dag["recipe"] == "demo-recipe"


def test_board_node_verb_runs_via_cli(home: Path, capsys) -> None:
    _seed(home)
    rc = board_cmd.main(["node", RUN, AGENT_NODE, "--view", "stream",
                         "--home", str(home)], "")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["run"] == RUN and out["node"] == AGENT_NODE


def test_board_steer_writes_a_row(home: Path, capsys) -> None:
    _seed(home)
    rc = board_cmd.main(["steer", RUN, "--role", "implementer",
                         "--severity", "info", "--text", "watch the tests",
                         "--home", str(home)], "")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["run_id"] == RUN and payload["role_target"] == "implementer"
    con = sqlite3.connect(home / "state.db")
    rows = con.execute(
        "SELECT message, role_target, severity FROM operator_steering WHERE run_id = ?",
        (RUN,)).fetchall()
    con.close()
    assert any(r[0] == "watch the tests" for r in rows)


def test_board_steer_rejects_bad_severity(home: Path, capsys) -> None:
    _seed(home)
    rc = board_cmd.main(["steer", RUN, "--role", "implementer",
                         "--severity", "bogus", "--text", "x",
                         "--home", str(home)], "")
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "severity" in out["error"]


def test_board_node_unknown_run_exits_1(home: Path, capsys) -> None:
    _seed(home)
    rc = board_cmd.main(["node", "run-nope", AGENT_NODE,
                         "--home", str(home)], "")
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False