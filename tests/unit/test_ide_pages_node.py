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
from mini_ork.ide_pages.node import (
    _call_for_node, _fetch_steer_rows, _node_tokens, build_node,
)
from mini_ork.ide_pages.run import _epoch, _load
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
    # r3 fix #3: every line carries a ``timestamp`` (ISO) so ``_stream_entries``
    # can order by real time instead of ``node_start + idx*1000``.
    session_path = run_dir / "sessions" / f"{SESSION_UUID}.jsonl"
    session_path.parent.mkdir(parents=True)
    session_path.write_text("\n".join([
        json.dumps({"type": "user", "timestamp": _iso(T0 + 10), "message": {"content": [
            {"type": "text", "text": "implement the fix"}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 11), "message": {"content": [
            {"type": "thinking", "thinking": "Let me read the file first."}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 12), "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Read",
             "input": {"file_path": "/tmp/x.py"}}
        ]}}),
        json.dumps({"type": "user", "timestamp": _iso(T0 + 13), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": [
                {"type": "text", "text": "def foo(): pass"}
            ], "is_error": False}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 14), "message": {"content": [
            {"type": "tool_use", "id": "toolu_2", "name": "Edit",
             "input": {"file_path": "/tmp/x.py",
                       "old_string": "def foo(): pass",
                       "new_string": "def foo(): return 42"}}
        ]}}),
        json.dumps({"type": "user", "timestamp": _iso(T0 + 15), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": [
                {"type": "text", "text": "applied"}
            ], "is_error": False}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 16), "message": {"content": [
            {"type": "tool_use", "id": "toolu_3", "name": "Bash",
             "input": {"command": "pytest"}}
        ]}}),
        json.dumps({"type": "user", "timestamp": _iso(T0 + 17), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_3", "content": [
                {"type": "text", "text": "ERROR: 1 failed"}
            ], "is_error": True}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 18), "message": {"content": [
            {"type": "tool_use", "id": "toolu_4", "name": "TodoWrite",
             "input": {"todos": [
                {"content": "read x.py", "status": "completed"},
                {"content": "edit x.py", "status": "in_progress"},
             ]}}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 19), "message": {"content": [
            {"type": "text", "text": "I edited the file and ran pytest."}
        ]}}),
        json.dumps({"type": "result", "timestamp": _iso(T0 + 20), "result": "done",
                    "session_id": SESSION_UUID,
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

    # Operator-steering row for the run.
    # r3 fix #3: park the steer at ``node.start + 30 s`` so it sorts AFTER
    # the last transcript line (T0+20) — the kickoff's realistic timing.
    # expires_at uses wall-clock now + 1 h so the ``expires_at > now`` filter
    # in ``_fetch_steer_rows`` always passes regardless of when the test runs.
    con = sqlite3.connect(home / "state.db")
    node_start_ms = (T0 + 10) * 1000
    steer_ms = node_start_ms + 30_000
    expires_ms = int(time.time() * 1000) + 3600_000
    con.execute(
        "INSERT INTO operator_steering (run_id, role_target, severity, message, source, "
        "confidence, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (RUN, AGENT_NODE, "info", "be careful with the Edit tool", "ide",
         0.8, steer_ms, expires_ms))
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
    # user → think → tool(Read) → tool(Edit) → tool(Bash) → todo → text → note.
    # r3 fix #3: the default fixture steer parks at ``node.start + 30 s``
    # (kickoff's realistic timing) which is past the transcript's last line
    # — that steer surfaces only when new transcript lines catch up to it,
    # so the kinds list stops at the result note.
    assert kinds == ["user", "think", "tool", "tool", "tool", "todo", "text", "note"]
    # r3 fix #7: status carries the TOTAL transcript entry count, not the
    # poll slice. 8 parsed entries (user, think, tool×3, todo, text, note).
    assert out["status"] == "finished · 8 events"
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
    """Kickoff r2 fix #3 — offset is now transcript-line count, not list index.

    First poll returns every entry (offset=N for N transcript lines).
    A second poll with ``offset=N`` returns nothing — every transcript line
    has been consumed, and the steer row's timestamp is at or after the
    line-at-N boundary so it is filtered out.
    """
    _seed(home)
    full = build_node(home, RUN, AGENT_NODE, view="stream")
    n_transcript_lines = full["offset"]
    # First poll consumed all transcript lines. Second poll at that offset
    # is empty; appending two assistant text lines afterwards yields exactly
    # those two on the next poll (next test).
    drained = build_node(home, RUN, AGENT_NODE, view="stream",
                         offset=n_transcript_lines)
    assert drained["entries"] == []
    assert drained["offset"] == n_transcript_lines


def test_stream_view_offset_after_append_returns_only_new_lines(home: Path) -> None:
    """Append-only invariant from kickoff r2 fix #3.

    Poll → offset N. Append two assistant text lines to the transcript.
    Poll with ``offset=N`` → exactly those two, and no repeated steer row.
    """
    _seed(home)
    initial = build_node(home, RUN, AGENT_NODE, view="stream")
    n = initial["offset"]
    assert initial["entries"], "fixture must yield at least one entry"

    # Append two assistant text lines to the existing session jsonl.
    session_path = (home / "runs" / RUN / "sessions" / f"{SESSION_UUID}.jsonl")
    with session_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "appended line A"}
        ]}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "appended line B"}
        ]}}) + "\n")

    after = build_node(home, RUN, AGENT_NODE, view="stream", offset=n)
    kinds = [e["k"] for e in after["entries"]]
    args = [e["arg"] for e in after["entries"]]
    assert kinds == ["text", "text"]
    assert args == ["appended line A", "appended line B"]
    # No repeated steer row: kickoff says "newer than the line at N".
    assert all(e["k"] != "steer" for e in after["entries"])
    assert after["offset"] == n + 2


def test_stream_view_prompt_view_renders_str_content(home: Path) -> None:
    """Kickoff r2 fix #1 — ``content`` may be a plain ``str`` (the prompt).

    A transcript whose first user message is a string ``content`` must
    surface that prompt in BOTH the stream view (first ``user`` entry)
    and the prompt view (rendered prompt block).
    """
    _seed(home)
    session_path = (home / "runs" / RUN / "sessions" / f"{SESSION_UUID}.jsonl")
    # Replace the first user entry with a plain-string-content variant.
    text = session_path.read_text(encoding="utf-8")
    _, _, rest = text.partition("\n")
    replacement = json.dumps({
        "type": "user",
        "message": {"content": "PROMPT AS STRING"},
    })
    session_path.write_text("\n".join([replacement, rest]))

    # Stream view: first entry kind is ``user`` with that prompt as arg.
    out = build_node(home, RUN, AGENT_NODE, view="stream")
    user = next(e for e in out["entries"] if e["k"] == "user")
    assert user["arg"] == "PROMPT AS STRING"
    # A following Read tool entry is still present (the second assistant).
    assert any(e["k"] == "tool" for e in out["entries"])

    # Prompt view: the prompt block contains the prompt text.
    prompt = build_node(home, RUN, AGENT_NODE, view="prompt")
    rendered = "\n".join(line["t"] for line in prompt["block"])
    assert "PROMPT AS STRING" in rendered


def test_stream_view_shell_node_uses_log_not_transcript(home: Path) -> None:
    """Kickoff r2 fix #2 — shell nodes resolve to their log, not a transcript."""
    _seed(home)
    out = build_node(home, RUN, SHELL_NODE, view="stream")
    assert out["ok"] is True
    # r3 fix #4: each log line is its own entry (per-line offset). The
    # shell-node log starts with "[verifier] running" then "[ok] verifier pass".
    texts = [e["arg"] for e in out["entries"] if e["k"] == "text"]
    assert texts == ["[verifier] running", "[ok] verifier pass"]
    assert out["offset"] == 2  # 2 log lines consumed
    # Resolver rule 3 only fires for running nodes; the shell node has no
    # node_end event in the fixture (state stays "running"), but it has no
    # session file at all — so the source must remain the verifier log,
    # not a transcript. Add a stray session to prove rule 3 is gated.
    stray = home / "runs" / RUN / "sessions" / "99999999-8888-7777-6666-555555555555.jsonl"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text(json.dumps({
        "type": "user", "message": {"content": "STRANGER SESSION"},
    }) + "\n")
    out2 = build_node(home, RUN, SHELL_NODE, view="stream")
    user_entries = [e for e in out2["entries"] if e["k"] == "user"]
    assert user_entries == []
    # Source pointer is the log filename, not the stray session file.
    assert out2["source"].endswith("verifier_verifier_node.log")


def test_stream_view_cost_state_builds_final_note(home: Path) -> None:
    """Kickoff r2 fix #5 — when the transcript has no ``result`` entry,
    the final note is built from the ``agent-<node>.live.jsonl`` cost-state.
    """
    _seed(home)
    session_path = (home / "runs" / RUN / "sessions" / f"{SESSION_UUID}.jsonl")
    # Strip the result entry from the fixture transcript.
    text = session_path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()
             and json.loads(ln).get("type") != "result"]
    session_path.write_text("\n".join(lines) + "\n")

    out = build_node(home, RUN, AGENT_NODE, view="stream")
    notes = [e for e in out["entries"] if e["k"] == "note"]
    assert len(notes) == 1, f"expected one note, got {len(notes)}"
    note = notes[0]
    assert "Done" in note["arg"]
    assert "$" in note["arg"]
    assert "turns" in note["arg"]


def test_stream_view_steer_entry_shows_severity_and_message(home: Path) -> None:
    """r3 fix #3: steer rows surface when within the transcript's timeline.

    The default fixture's steer parks at ``node.start + 30 s`` (kickoff's
    realistic timing, past the last transcript line) — that row is shown only
    when the transcript catches up to it. Inject an additional steer at
    ``node.start + 5 s`` so the test exercises the in-range emission path.
    """
    _seed(home)
    con = sqlite3.connect(home / "state.db")
    in_range_ms = (T0 + 10 + 5) * 1000  # node.start=T0+10; +5 s
    expires_ms = int(time.time() * 1000) + 3600_000
    con.execute(
        "INSERT INTO operator_steering (run_id, role_target, severity, message, source, "
        "confidence, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (RUN, "implementer", "info", "be careful with the Edit tool", "ide",
         0.8, in_range_ms, expires_ms))
    con.commit()
    con.close()

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


# ── r3 fixes — fail-before / pass-after evidence (kickoff lines 9-39) ──────


def test_call_for_node_uses_iso_ts_to_epoch_seconds(home: Path) -> None:
    """r3 fix #1: ``_call_for_node`` parses ``ts`` ISO via ``_epoch`` (seconds).

    Before the fix the row carried ``ts_ms`` (None) and the comparison floor
    collapsed every call, so telemetry + tokens silently dropped.
    """
    _seed(home)
    run_obj = _load(home, RUN)
    node = next(n for n in run_obj.nodes if n.id == AGENT_NODE)
    # node.start = T0+10, node.end = T0+60; window is [T0+8, T0+65] seconds.
    inside = {"actor": "worker", "ts": _iso(T0 + 50),
              "input_tokens": 100, "output_tokens": 50}
    assert _call_for_node(inside, node) is True
    # Clearly outside the window → no match.
    outside = dict(inside, ts=_iso(T0 - 100))
    assert _call_for_node(outside, node) is False
    # Wrong actor → no match.
    wrong = dict(inside, actor="reviewer")
    assert _call_for_node(wrong, node) is False
    # ``ts`` must be the ISO column — the legacy ``ts_ms`` field is ignored.
    legacy = {"actor": "worker", "ts_ms": (T0 + 50) * 1000,
              "input_tokens": 1, "output_tokens": 1}
    assert _call_for_node(legacy, node) is False


def test_telemetry_includes_node_call_via_iso_window(home: Path) -> None:
    """r3 fix #1: the seeded ``llm_calls`` row lands inside the telemetry table."""
    _seed(home)
    out = build_node(home, RUN, AGENT_NODE, view="telemetry")
    # block rows = [head, row1, ...]. At least one non-head row.
    assert len(out["block"]) >= 2
    body_rows = out["block"][1:]
    assert any(r[1]["t"] != "—" for r in body_rows), body_rows


def test_node_tokens_input_plus_output_not_cached(home: Path) -> None:
    """r3 fix #7 minor: ``_node_tokens`` sums input + output; cached excluded."""
    _seed(home)
    run_obj = _load(home, RUN)
    node = next(n for n in run_obj.nodes if n.id == AGENT_NODE)
    # Inject an attributed call with all three token columns.
    run_obj.calls = [
        {"actor": "worker", "ts": _iso(T0 + 50),
         "input_tokens": 700, "output_tokens": 300,
         "cached_input_tokens": 999, "cost_usd": 0.01},
    ]
    # 700 + 300 = 1000; cached (999) must NOT be summed.
    assert _node_tokens(node, run_obj) == 1000


def test_status_finished_event_count_is_total_transcript(home: Path) -> None:
    """r3 fix #7 minor: ``finished · N events`` counts ALL transcript entries,
    not just this poll's slice."""
    _seed(home)
    full = build_node(home, RUN, AGENT_NODE, view="stream")
    # Fixture has 8 parsed transcript entries (user, think, tool×3, todo, text,
    # result-note). ``finished · N events`` reflects the transcript, not the
    # poll slice (which would add a steer row, but the fixture's steer parks
    # at ``node.start + 30 s`` — past the transcript's last line).
    assert full["status"] == "finished · 8 events"
    # And a follow-up poll at the same boundary reports the same count.
    drained = build_node(home, RUN, AGENT_NODE, view="stream",
                         offset=full["offset"])
    assert drained["status"] == "finished · 8 events"


def test_offset_no_repeated_note_on_repeated_polls(home: Path) -> None:
    """r3 fix #2: 10-line transcript (no ``result``) → offsets 10/10/10 on
    three polls; polls 2/3 return nothing. Append 2 lines → returns those 2.

    The cost-state note is emitted ONCE on the full read and does NOT advance
    ``offset``; subsequent polls at the same offset return nothing and never
    re-synthesise the note.
    """
    _seed(home)
    session_path = (home / "runs" / RUN / "sessions" / f"{SESSION_UUID}.jsonl")
    # Strip the result entry → 10 transcript lines, no result of its own.
    text = session_path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()
             and json.loads(ln).get("type") != "result"]
    session_path.write_text("\n".join(lines) + "\n")

    full = build_node(home, RUN, AGENT_NODE, view="stream")
    n = full["offset"]
    assert n == 10, f"expected offset 10, got {n}"
    # Exactly one cost-state note, and it does NOT count in n.
    notes = [e for e in full["entries"] if e["k"] == "note"]
    assert len(notes) == 1, f"expected one note, got {len(notes)}"

    drained = build_node(home, RUN, AGENT_NODE, view="stream", offset=n)
    assert drained["entries"] == []
    assert drained["offset"] == n
    # Third poll: same.
    drained3 = build_node(home, RUN, AGENT_NODE, view="stream", offset=n)
    assert drained3["entries"] == []
    assert drained3["offset"] == n

    # Append 2 lines → poll at offset=n returns exactly those 2.
    with session_path.open("a", encoding="utf-8") as f:
        for txt in ("appended line A", "appended line B"):
            f.write(json.dumps({"type": "assistant", "timestamp": _iso(T0 + 25),
                                "message": {"content": [
                                    {"type": "text", "text": txt}]}}) + "\n")
    after = build_node(home, RUN, AGENT_NODE, view="stream", offset=n)
    kinds = [e["k"] for e in after["entries"]]
    args = [e["arg"] for e in after["entries"]]
    assert kinds == ["text", "text"]
    assert args == ["appended line A", "appended line B"]
    assert after["offset"] == n + 2


def test_steering_orders_by_iso_transcript_timestamp(home: Path) -> None:
    """r3 fix #3: steer rows ordered by ISO timestamp from transcript lines.

    A steer row BEFORE ``node.start`` is shown at the top on full read; rows
    are NOT re-emitted on subsequent polls at the same offset.
    """
    _seed(home)
    session_path = (home / "runs" / RUN / "sessions" / f"{SESSION_UUID}.jsonl")
    # Strip the result line so we can see the prior-steer above the first user.
    text = session_path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()
             and json.loads(ln).get("type") != "result"]
    session_path.write_text("\n".join(lines) + "\n")

    # Inject a steer row BEFORE node.start (node.start = T0+10).
    con = sqlite3.connect(home / "state.db")
    prior_ms = (T0 + 5) * 1000
    expires_ms = int(time.time() * 1000) + 3600_000
    con.execute(
        "INSERT INTO operator_steering (run_id, role_target, severity, message, source, "
        "confidence, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (RUN, "implementer", "info", "prior steering", "ide",
         0.8, prior_ms, expires_ms))
    con.commit()
    con.close()

    out = build_node(home, RUN, AGENT_NODE, view="stream")
    # First entry is the prior steer (T0+5 < first transcript line at T0+10).
    assert out["entries"][0]["k"] == "steer"
    assert "prior steering" in out["entries"][0]["arg"]
    # No duplicate steer on subsequent polls at the same offset.
    drained = build_node(home, RUN, AGENT_NODE, view="stream",
                         offset=out["offset"])
    assert all(e["k"] != "steer" for e in drained["entries"])


def test_shell_log_offset_returns_only_new_lines(home: Path) -> None:
    """r3 fix #4: append a line to the verifier log → poll at previous offset
    returns only the new line."""
    _seed(home)
    log = home / "runs" / RUN / f"verifier_{SHELL_NODE}.log"
    full = build_node(home, RUN, SHELL_NODE, view="stream")
    initial_offset = full["offset"]
    # Fixture has 2 log lines → offset 2.
    assert initial_offset == 2

    # Append a new line and poll at the previous offset.
    with log.open("a", encoding="utf-8") as f:
        f.write("[info] appended line\n")
    after = build_node(home, RUN, SHELL_NODE, view="stream", offset=initial_offset)
    texts = [e["arg"] for e in after["entries"] if e["k"] == "text"]
    assert texts == ["[info] appended line"]
    assert after["offset"] == initial_offset + 1


def test_fetch_steer_rows_uses_mapped_role(home: Path) -> None:
    """r3 fix #5: ``_fetch_steer_rows`` filters on mapped ROLE, not on ``node_id``.

    Before the fix the reader passed the node's id as the filter; rows whose
    ``role_target`` was a valid role but did not match the id were silently
    dropped. After the fix ``role_target='verifier'`` and ``role_target='any'``
    match a static_check (verifier) node; ``role_target='reviewer'`` does not.
    """
    _seed(home)
    con = sqlite3.connect(home / "state.db")
    expires_ms = int(time.time() * 1000) + 3600_000
    for role, msg in (("verifier", "for verifier"),
                      ("any", "for any"),
                      ("reviewer", "for reviewer")):
        con.execute(
            "INSERT INTO operator_steering (run_id, role_target, severity, message, source, "
            "confidence, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (RUN, role, "info", msg, "ide", 0.8, (T0 + 30) * 1000, expires_ms))
    con.commit()
    con.close()

    rows = _fetch_steer_rows(RUN, home, role="verifier")
    msgs = [r.get("message") for r in rows]
    assert "for verifier" in msgs
    assert "for any" in msgs
    assert "for reviewer" not in msgs


def test_learning_view_gradient_window_uses_seconds(home: Path) -> None:
    """r3 fix #6: gradient_records.created_at is epoch SECONDS.

    node.start=T0+10, node.end=T0+60 → window [T0+10, T0+120]. A row at
    T0+30 (in window) is returned; rows outside are not.
    """
    _seed(home)
    con = sqlite3.connect(home / "state.db")
    for gid, sig, at in (("gr-inside", "in-window", T0 + 30),
                          ("gr-before", "before-window", T0 - 100),
                          ("gr-after", "after-window", T0 + 500)):
        con.execute(
            "INSERT INTO gradient_records (gradient_id, target, signal, "
            "task_class, confidence, created_at, suggested_change, evidence) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (gid, "demo", sig, "demo", 0.9, at, "", "test-fixture"))
    con.commit()
    con.close()

    out = build_node(home, RUN, AGENT_NODE, view="learning")
    items_str = " ".join(str(item) for item in out["list"])
    assert "gr-inside" in items_str
    assert "gr-before" not in items_str
    assert "gr-after" not in items_str
