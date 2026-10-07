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
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 70), "message": {"content": [
            {"type": "thinking", "thinking": "Let me read the file first."}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 130), "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Read",
             "input": {"file_path": "/tmp/x.py"}}
        ]}}),
        json.dumps({"type": "user", "timestamp": _iso(T0 + 190), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": [
                {"type": "text", "text": "def foo(): pass"}
            ], "is_error": False}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 250), "message": {"content": [
            {"type": "tool_use", "id": "toolu_2", "name": "Edit",
             "input": {"file_path": "/tmp/x.py",
                       "old_string": "def foo(): pass",
                       "new_string": "def foo(): return 42"}}
        ]}}),
        json.dumps({"type": "user", "timestamp": _iso(T0 + 310), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": [
                {"type": "text", "text": "applied"}
            ], "is_error": False}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 370), "message": {"content": [
            {"type": "tool_use", "id": "toolu_3", "name": "Bash",
             "input": {"command": "pytest"}}
        ]}}),
        json.dumps({"type": "user", "timestamp": _iso(T0 + 430), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_3", "content": [
                {"type": "text", "text": "ERROR: 1 failed"}
            ], "is_error": True}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 490), "message": {"content": [
            {"type": "tool_use", "id": "toolu_4", "name": "TodoWrite",
             "input": {"todos": [
                {"content": "read x.py", "status": "completed"},
                {"content": "edit x.py", "status": "in_progress"},
             ]}}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 550), "message": {"content": [
            {"type": "text", "text": "I edited the file and ran pytest."}
        ]}}),
        json.dumps({"type": "result", "timestamp": _iso(T0 + 610), "result": "done",
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
    # expires_at uses wall-clock now + 1 h so the ``expires_at > now`` filter
    # in ``_fetch_steer_rows`` always passes regardless of when the test runs.
    con = sqlite3.connect(home / "state.db")
    # r5: with 60-s spaced transcript lines, park the default steer at
    # T0+700 (= last line at T0+610 + buffer) so it sorts AFTER the
    # transcript's last line on a full read — preserves the r3 property
    # "kinds list stops at the result note" the kinds-order test asserts.
    steer_ms = (T0 + 700) * 1000
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
    # The fixture's steer is timed after the transcript's last line, so it
    # surfaces only when new transcript lines catch up to it.
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
    """r4 fix #4: 10-line transcript (no ``result``) → full read returns 10
    transcript entries plus the cost-state note; ``next_offset`` stays at
    10 because the note does NOT advance the cursor (kickoff "without
    changing offset"). A per-node marker file guarantees the note is
    emitted at most once — subsequent polls at the same offset return
    empty.

    Appending 2 lines and polling at the same offset returns those 2
    because the filter still matches ``_line >= offset`` for them.
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
    # Exactly one cost-state note. r4: the note does NOT count in the
    # status pill's ``transcript_entry_count`` (which counts emitted
    # entries only) and does NOT advance ``next_offset``.
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


# ── r4 fixes — fail-before / pass-after evidence (kickoff ide-node-stream-r4) ──


def _write_five_line_transcript(home: Path) -> Path:
    """Replace the shared fixture's session with 5 lines at 60-s spacing.

    Returns the session path. Used by the r5 steer tests that exercise
    the r4 real-``_ts`` lower-bound fix (with 60-s spacing the proxy
    ``node_start + idx`` collapses to a wrong answer, exposing the fix).
    The default ``_seed`` already writes 60-s spaced lines, but with
    11 of them; these tests want a tight 5-line fixture.
    """
    run_dir = home / "runs" / RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    session_path = run_dir / "sessions" / f"{SESSION_UUID}.jsonl"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text("\n".join([
        json.dumps({"type": "user", "timestamp": _iso(T0 + 10), "message": {"content": [
            {"type": "text", "text": "implement the fix"}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 70), "message": {"content": [
            {"type": "thinking", "thinking": "step 1"}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 130), "message": {"content": [
            {"type": "text", "text": "step 2"}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 190), "message": {"content": [
            {"type": "text", "text": "step 3"}
        ]}}),
        json.dumps({"type": "assistant", "timestamp": _iso(T0 + 250), "message": {"content": [
            {"type": "text", "text": "step 4"}
        ]}}),
    ]) + "\n")
    # Strip the result entry so the cost-state note path doesn't see one.
    return session_path


def test_steer_lower_bound_uses_real_iso_ts_not_proxy(home: Path) -> None:
    """r4 fix #1: steer emitted iff ``steer_ts > max(_ts of consumed lines)``.

    The kickoff distinguishes the OLD proxy ``node_start + prev_line_idx``
    from the NEW real bound by their behaviour on a 60-s-spaced
    transcript. With the OLD proxy:

    * offset=2, consumed lines are 0 (T0+10) and 1 (T0+70).
    * Proxy lower bound = ``node_start + (offset-1) = T0+10 + 1 = T0+11``.
    * A steer at T0+60 (> T0+11, < T0+70) passes the OLD lower bound.

    With the NEW real bound:

    * Real lower bound = ``max(_ts of lines 0,1) = T0+70``.
    * A steer at T0+60 fails the strict ``> T0+70`` check → dropped.

    The OLD code emits the steer; the NEW code does not. The test is
    fail-before/pass-after for r4 fix #1.
    """
    _seed(home)
    _write_five_line_transcript(home)
    # Steer at T0+60 — strictly between line 0's _ts (T0+10) and line 1's
    # _ts (T0+70). Passes the proxy lower bound (T0+11), fails the real
    # lower bound (T0+70).
    con = sqlite3.connect(home / "state.db")
    expires_ms = int(time.time() * 1000) + 3600_000
    con.execute(
        "INSERT INTO operator_steering (run_id, role_target, severity, message, source, "
        "confidence, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (RUN, "implementer", "info", "between-line-0-and-1", "ide",
         0.8, (T0 + 60) * 1000, expires_ms))
    con.commit()
    con.close()

    # Poll at offset=2: lines 0..1 consumed; lines 2..4 to-consume.
    # OLD: steer 60 > proxy 11 → pass; 60 ≤ upper 250 → emit.
    # NEW: steer 60 > real 70 → FAIL → skip.
    out = build_node(home, RUN, AGENT_NODE, view="stream", offset=2)
    steers = [e["arg"] for e in out["entries"] if e["k"] == "steer"]
    assert "between-line-0-and-1" not in " ".join(steers), (
        f"r4 fix #1 failed: steer at T0+60 between consumed lines should "
        f"be filtered by the real bound; emitted entries: {steers}"
    )


def test_steer_emitted_once_and_skipped_on_subsequent_polls(home: Path) -> None:
    """r5 fix #1 (positive case, kickoff scenario verbatim).

    Transcript: 5 lines spaced 60 s apart (T0+10, 70, 130, 190, 250).
    Steer at ``start+100 s`` = T0+110 (between line 1 at T0+70 and line
    2 at T0+130).

    * Poll 1 (full read, offset=0): lower=None, upper=T0+250, steer
      110 ≤ 250 → emit. next_offset = 5.
    * Append "line 5" to the transcript at T0+260 (BEFORE the steer's
      position is consumed). Poll 2 at the previous offset (5):
      consumed lines 0..4 → real lower = T0+250; new line at T0+260
      → has_upper=True; max_upper_ts = T0+260. The steer at T0+110 is
      ≤ max_lower_ts (T0+250) → skipped. Only the appended line
      surfaces.
    """
    _seed(home)
    session_path = _write_five_line_transcript(home)
    con = sqlite3.connect(home / "state.db")
    expires_ms = int(time.time() * 1000) + 3600_000
    con.execute(
        "INSERT INTO operator_steering (run_id, role_target, severity, message, source, "
        "confidence, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (RUN, "implementer", "info", "mid-stream steer", "ide", 0.8,
         (T0 + 110) * 1000, expires_ms))
    con.commit()
    con.close()

    # Poll 1: full read emits the steer (lower=None on offset=0; 110 ≤ 250).
    full = build_node(home, RUN, AGENT_NODE, view="stream")
    full_steers = [e["arg"] for e in full["entries"] if e["k"] == "steer"]
    assert any("mid-stream steer" in s for s in full_steers), (
        f"expected the mid-stream steer to emit on full read; got: "
        f"{[e for e in full['entries'] if e['k'] == 'steer']}"
    )
    full_offset = full["offset"]

    # Append a line BETWEEN polls.
    with session_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": _iso(T0 + 260),
                            "message": {"content": [
                                {"type": "text", "text": "appended line"}
                            ]}}) + "\n")

    # Poll 2 at the previous offset: only the appended line surfaces;
    # the steer is dropped by the real-``_ts`` lower bound.
    after = build_node(home, RUN, AGENT_NODE, view="stream", offset=full_offset)
    args = [e["arg"] for e in after["entries"]]
    kinds = [e["k"] for e in after["entries"]]
    assert "appended line" in args, f"expected appended line, got: {args}"
    assert all("mid-stream steer" not in (a or "") for a in args), (
        f"steer must NOT re-emit after the consumed-``_ts`` lower bound "
        f"advances to T0+250; entries: {after['entries']}"
    )
    assert "steer" not in kinds, (
        f"no steer row expected on the post-append poll; got kinds: {kinds}"
    )
    assert after["offset"] == full_offset + 1


def test_proxy_lower_bound_is_gated_on_log_backed_stream(home: Path) -> None:
    """r6 fix #1: the log-line proxy (``node_start + offset - 1``) must only
    enter ``lower_bound_ts`` when the stream is genuinely log-backed.

    Recipe: an implementer node has BOTH a session transcript AND an
    ``impl-<node>.log``. On HEAD (r5), ``_stream_entries`` appended the
    proxy to ``lower_bound_ts`` whenever ``log_path is not None``, even
    when ``session_path is not None`` — pushing ``max_lower_ts`` forward
    to ``T0+109`` and causing a late-arriving steer at ``T0+25`` to be
    dropped on the post-append poll.

    Build: 100 transcript lines packed into whole seconds (T0+10 ..
    T0+19.9), an ``impl-<node>.log``, a steer at T0+25. Append one
    catch-up transcript line at T0+30 (idx 100). Poll at offset=100.

    * OLD (HEAD, buggy): max_lower_ts = max(T0+19.9, T0+109) = T0+109.
      Steer 25 ≤ 109 → dropped. Test FAILS.
    * NEW (r6 fix): max_lower_ts = T0+19.9 (real ISO wins). Steer
      25 > 19.9 AND 25 ≤ 30 → emitted exactly once. Test PASSES.
    """
    _seed(home)
    run_dir = home / "runs" / RUN
    session_path = _write_five_line_transcript(home)

    # Implementer's own log — drives ``log_path is not None`` while a
    # transcript is also present, exactly the r5 regression shape.
    (run_dir / f"impl-{AGENT_NODE}.log").write_text(
        "[impl] starting\n[impl] step 1\n[impl] step 2\n")

    # Replace the fixture's 5-line transcript with a 100-line dense one
    # packed across 10 s (10 lines per integer second, T0+10..T0+19).
    # The kickoff asks for the bug to fail-before: with the proxy
    # injected, ``max_lower_ts`` jumps to ``node_start + offset - 1
    # = T0+109``, which is later than every real consumed line. The
    # ISO timestamp format truncates to whole seconds, so the "density"
    # of the test is achieved by ensuring the transcript's max real
    # ``_ts`` stays well under T0+109.
    dense_lines: list[str] = []
    for i in range(100):
        ts = T0 + 10 + (i // 10)  # 10 lines per integer second
        dense_lines.append(json.dumps({
            "type": "assistant",
            "timestamp": _iso(ts),
            "message": {"content": [
                {"type": "text", "text": f"line {i}"}
            ]},
        }))
    session_path.write_text("\n".join(dense_lines) + "\n")

    # Steer at T0+25 — past the transcript's last line (T0+19.9) but
    # BEFORE the appended catch-up line at T0+30. With the bug, the
    # steer is dropped on every post-append poll.
    con = sqlite3.connect(home / "state.db")
    expires_ms = int(time.time() * 1000) + 3600_000
    con.execute(
        "INSERT INTO operator_steering (run_id, role_target, severity, message, source, "
        "confidence, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (RUN, AGENT_NODE, "info", "r6 fix #1 regression", "ide", 0.8,
         (T0 + 25) * 1000, expires_ms))
    con.commit()
    con.close()

    # Append the catch-up transcript line at T0+30 (line idx 100).
    with session_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": _iso(T0 + 30),
                            "message": {"content": [
                                {"type": "text", "text": "catch-up"}
                            ]}}) + "\n")

    # Poll at offset=100: lines 0..99 consumed; appended line in upper.
    out = build_node(home, RUN, AGENT_NODE, view="stream", offset=100)
    args = [e["arg"] for e in out["entries"]]
    steer_rows = [e for e in out["entries"] if e["k"] == "steer"]
    assert len(steer_rows) == 1, (
        f"r6 fix #1 failed: expected exactly one steer emit on the "
        f"post-append poll; got {len(steer_rows)} (entries: {args})"
    )
    assert "r6 fix #1 regression" in (steer_rows[0]["arg"] or ""), (
        f"r6 fix #1 failed: steer payload missing; got: {steer_rows[0]}"
    )
    assert "catch-up" in args, (
        f"r6 fix #1 failed: appended line must surface; got: {args}"
    )


def test_shell_log_uses_absolute_line_indices(home: Path) -> None:
    """r4 fix #2: 45-line log → offset 45; append "NEW LINE" → poll at 45
    returns exactly that line.

    The full read caps to the last 40 lines (with absolute ``_line``
    5..44). next_offset = 45 (total physical line count). A subsequent
    poll at offset=45 returns empty; appending "NEW LINE" at line 45
    and polling at offset=45 returns exactly that one line.
    """
    _seed(home)
    log = home / "runs" / RUN / f"verifier_{SHELL_NODE}.log"
    # Write 45 lines: lines 0..39 plus 5 more (lines 40..44).
    lines = [f"line {i}" for i in range(45)]
    log.write_text("\n".join(lines) + "\n")

    # Full read: capped to last 40 lines (absolute indices 5..44).
    full = build_node(home, RUN, SHELL_NODE, view="stream")
    full_texts = [e["arg"] for e in full["entries"] if e["k"] == "text"]
    assert len(full_texts) == 40, f"expected 40 lines on full read, got {len(full_texts)}"
    # First emitted absolute line index is 5 ("line 5"); last is 44 ("line 44").
    assert full_texts[0] == "line 5"
    assert full_texts[-1] == "line 44"
    # next_offset = 45 (total physical line count, NOT the cap).
    assert full["offset"] == 45, f"expected offset 45, got {full['offset']}"

    # Drain at offset=45: empty.
    drained = build_node(home, RUN, SHELL_NODE, view="stream", offset=45)
    assert drained["entries"] == []

    # Append "NEW LINE" at absolute index 45.
    with log.open("a", encoding="utf-8") as f:
        f.write("NEW LINE\n")
    after = build_node(home, RUN, SHELL_NODE, view="stream", offset=45)
    after_texts = [e["arg"] for e in after["entries"] if e["k"] == "text"]
    assert after_texts == ["NEW LINE"], f"expected ['NEW LINE'], got {after_texts}"
    assert after["offset"] == 46, f"expected offset 46, got {after['offset']}"


def test_role_for_node_treats_lens_as_reviewer(home: Path) -> None:
    """r4 fix #3: ``researcher`` whose id ends in ``_lens`` → reviewer.

    Before the fix the map lacked explicit ``researcher`` handling, so a
    lens researcher only saw ``any``-targeted steers. After the fix a
    reviewer-targeted steer at the ``code_impact_lens`` node is
    surfaced by ``_role_for_node``.
    """
    from mini_ork.ide_pages.node import _role_for_node
    from mini_ork.ide_pages.run import _REVIEW_TYPES as _RT

    # Synthetic Node instances to exercise _role_for_node.
    code_impact = _make_node("code_impact_lens", "researcher")
    prior_art = _make_node("prior_art_lens", "researcher")
    plain_scout = _make_node("scout", "researcher")
    judge = _make_node("judge_node", "judge")
    synth = _make_node("synth_node", "synthesizer")
    lens = _make_node("lens_node", "lens")

    assert _role_for_node(code_impact) == "reviewer"
    assert _role_for_node(prior_art) == "reviewer"
    assert _role_for_node(plain_scout) == "any", (
        "non-lens researchers should fall through to the default role"
    )
    for node in (judge, synth, lens):
        assert _role_for_node(node) == "reviewer", (
            f"{node.id} (type={node.type}) should map to reviewer"
        )
    # Sanity: every reviewer-role node type is in _REVIEW_TYPES (per kickoff).
    for rt in _RT:
        n = _make_node(f"x_{rt}", rt)
        assert _role_for_node(n) == "reviewer", (
            f"_REVIEW_TYPES entry {rt!r} should map to reviewer"
        )


def _make_node(node_id: str, node_type: str):
    """Lightweight Node for ``_role_for_node`` tests (no DB needed)."""
    from mini_ork.ide_pages.run import Node
    return Node(id=node_id, type=node_type)


def test_cost_note_stateless_full_read_always_emits(home: Path) -> None:
    """r5 fix #2: a full read (offset=0) of a FINISHED node always emits
    the cost-state note — two viewers that open it cold both see it.

    The r4 marker-file mechanism is gone; the once-ness is reconstructed
    from data the builder already has (``offset==0`` path on a finished
    node). A fresh full read after a previous full read must still emit
    because there's no marker to consult — stateless derivation.
    """
    _seed(home)
    session_path = home / "runs" / RUN / "sessions" / f"{SESSION_UUID}.jsonl"
    # Strip the result entry so the cost-state fallback note is what feeds the note.
    text = session_path.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()
             and json.loads(ln).get("type") != "result"]
    session_path.write_text("\n".join(lines) + "\n")

    # First full read emits the note.
    first = build_node(home, RUN, AGENT_NODE, view="stream")
    notes_first = [e for e in first["entries"] if e["k"] == "note"]
    assert len(notes_first) == 1, (
        f"expected one cost-state note on first full read, got {len(notes_first)}"
    )
    assert "Done" in notes_first[0]["arg"] and "$" in notes_first[0]["arg"]

    # r5 invariant: NO marker file is written. The read path is read-only.
    marker = home / "runs" / RUN / f".cost-note-emitted.{AGENT_NODE}"
    assert not marker.exists(), (
        f"r5: read path must never write a marker file; found {marker}"
    )

    # A SECOND full read on the same view also emits the note — two
    # viewers both get it (kickoff item 2: "Two full reads by two viewers
    # both get the note").
    second = build_node(home, RUN, AGENT_NODE, view="stream")
    notes_second = [e for e in second["entries"] if e["k"] == "note"]
    assert len(notes_second) == 1, (
        f"r5: a fresh full read still emits the note (stateless); got {len(notes_second)}"
    )


def test_cost_note_stateless_live_follower_sees_once_on_edge(home: Path) -> None:
    """r5 fix #2 + fix #4: start polling while LIVE with no cost-state,
    then add cost-state and mark the node FINISHED. The next incremental
    poll returns the note once; the following one returns nothing; a
    fresh full read returns it again. The "once" guarantee on the
    incremental path is derived from data, not a marker file.
    """
    _seed(home, status="executing")
    session_path = home / "runs" / RUN / "sessions" / f"{SESSION_UUID}.jsonl"
    # Replace the default 10-line transcript (60-s spacing — last line
    # at T0+550) with a single live-phase line at T0+10. The kickoff's
    # past-edge predicate requires ``newest_consumed_ts < end`` (T0+60),
    # which the default transcript's later lines violate.
    session_path.write_text(json.dumps({
        "type": "user",
        "timestamp": _iso(T0 + 10),
        "message": {"content": [{"type": "text", "text": "fix"}]},
    }) + "\n")

    # Force AGENT_NODE into the "running" state so is_live=True on the
    # first poll. Delete the default node_end so the loader sees only
    # node_start.
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "DELETE FROM run_events WHERE run_id = ? AND event_type = 'node_end' "
        "AND json_extract(payload_json, '$.node_id') = ?",
        (RUN, AGENT_NODE))
    # Default _seed's llm_calls row has session_id=NULL; rule 1 of the
    # resolver reads live.jsonl which we delete. Give rule 2 a session_id.
    con.execute(
        "UPDATE llm_calls SET session_id = ? WHERE run_id = ? AND actor = 'worker'",
        (SESSION_UUID, RUN))
    con.commit(); con.close()

    # Drop the cost-state envelope so the live follower sees no note.
    live_path = home / "runs" / RUN / f"agent-{AGENT_NODE}.live.jsonl"
    live_path.unlink(missing_ok=True)

    # Poll 1 (live, no cost-state): no note.
    poll1 = build_node(home, RUN, AGENT_NODE, view="stream")
    notes1 = [e for e in poll1["entries"] if e["k"] == "note"]
    assert notes1 == [], (
        f"live poll should not emit a note without cost-state; got: {notes1}"
    )
    follow_offset = poll1["offset"]
    assert follow_offset == 1, f"expected 1 consumed line, got {follow_offset}"

    # Append a new transcript line PAST node.end (T0+60) — this is the
    # realistic "node just finished, an event was logged at end_time"
    # scenario the kickoff describes as the past-edge trigger.
    with session_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant",
                            "timestamp": _iso(T0 + 70),
                            "message": {"content": [
                                {"type": "text", "text": "edge-line"}
                            ]}}) + "\n")

    # Now add cost-state (live.jsonl envelope) and a node_end event so
    # ``_live`` flips to False (finished). The cost-state ts = T0+60.
    live_path.write_text(
        json.dumps({"seq": 0, "stream": "stdout", "t": T0 + 60,
                    "line": json.dumps({"session_id": SESSION_UUID,
                                        "total_cost_usd": 0.31, "num_turns": 12})}) + "\n"
    )
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "INSERT INTO run_events (event_id, run_id, event_type, payload_json, created_at) "
        "VALUES (?,?,?,?,?)",
        ("ev-finish-live", RUN, "node_end",
         json.dumps({"node_id": AGENT_NODE, "node_type": "implementer",
                     "model_lane": "worker", "finish_reason": "done"}),
         T0 + 60))
    con.commit(); con.close()

    # Poll 2 (incremental): first poll past the live→finished edge.
    # Predicate: newest_consumed_ts (T0+10) < cs_ts (T0+60) ✓ AND
    # max_upper_ts (T0+70) >= cs_ts (T0+60) ✓ → emit note.
    poll2 = build_node(home, RUN, AGENT_NODE, view="stream", offset=follow_offset)
    notes2 = [e for e in poll2["entries"] if e["k"] == "note"]
    assert len(notes2) == 1, (
        f"first incremental poll past the edge should emit the note once; "
        f"got {len(notes2)} notes"
    )
    edge_offset = poll2["offset"]

    # Poll 3 (incremental at the new offset, no further new lines): nothing.
    # Once-ness is reconstructed from the append-only offset contract — the
    # caller advances offset based on ``next_offset`` from the prior poll,
    # so the post-edge line is now "consumed" and the next ``_ste`` upper
    # bound is empty. Without that advance the predicate would fire again,
    # which is why the kickoff specifies the stateless design must rely on
    # the caller respecting the offset cursor (kickoff: "the read path must
    # never write").
    poll3 = build_node(home, RUN, AGENT_NODE, view="stream",
                       offset=edge_offset)
    notes3 = [e for e in poll3["entries"] if e["k"] == "note"]
    assert notes3 == [], (
        f"subsequent incremental poll (advanced offset) should not re-emit; "
        f"entries: {notes3}"
    )

    # Poll 4 (fresh full read): note returns (path (a) — every full read).
    poll4 = build_node(home, RUN, AGENT_NODE, view="stream")
    notes4 = [e for e in poll4["entries"] if e["k"] == "note"]
    assert len(notes4) == 1, (
        f"a fresh full read must re-emit the note; entries: {notes4}"
    )


def test_log_backed_steer_window_from_line_positions(home: Path) -> None:
    """r5 fix #1: log-backed nodes use log line positions for the steer
    window. A 10-line verifier log (each line carries no ISO timestamp,
    falls back to ``node_start + line_index`` seconds); a steer at
    T0+62 must emit on poll 1 and NOT emit on poll 2 after "line 10"
    is appended at T0+70.

    node.start = T0+60 (from events table). Line 0 → T0+60, …, line 9 →
    T0+69. Steer at T0+62 is strictly between line 2 (T0+62) and line
    3 (T0+63) — so it falls inside the consumed window after poll 1.

    Poll 1 (offset=0): consumed=[], upper=[T0+60..T0+69] → max_upper =
    T0+69. Steer 62 ≤ 69 → emit. After poll 1, append "line 10" → log
    has 11 lines (0..10), line 10 → T0+70.

    Poll 2 (offset=10): consumed=[T0+60..T0+69], upper=[T0+70]. Real
    lower = T0+69; steer 62 ≤ 69 → dropped.
    """
    _seed(home)
    log = home / "runs" / RUN / f"verifier_{SHELL_NODE}.log"
    lines = [f"line {i}" for i in range(10)]
    log.write_text("\n".join(lines) + "\n")

    # Inject a steer at T0+62.
    con = sqlite3.connect(home / "state.db")
    expires_ms = int(time.time() * 1000) + 3600_000
    con.execute(
        "INSERT INTO operator_steering (run_id, role_target, severity, message, source, "
        "confidence, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (RUN, "verifier", "info", "log-backed steer", "ide", 0.8,
         (T0 + 62) * 1000, expires_ms))
    con.commit()
    con.close()

    # Poll 1: full read emits the steer.
    full = build_node(home, RUN, SHELL_NODE, view="stream")
    steers = [e["arg"] for e in full["entries"] if e["k"] == "steer"]
    assert any("log-backed steer" in s for s in steers), (
        f"expected log-backed steer on poll 1; entries: {full['entries']}"
    )
    full_offset = full["offset"]
    assert full_offset == 10, f"expected offset 10, got {full_offset}"

    # Append "line 10" at T0+70.
    with log.open("a", encoding="utf-8") as f:
        f.write("line 10\n")

    # Poll 2 at the previous offset: consumed=[T0+60..T0+69], new upper
    # = T0+70. The steer's position (T0+62) is inside the consumed
    # window → dropped.
    after = build_node(home, RUN, SHELL_NODE, view="stream",
                       offset=full_offset)
    args = [e["arg"] for e in after["entries"]]
    kinds = [e["k"] for e in after["entries"]]
    assert "line 10" in args, f"expected appended line, got: {args}"
    assert "steer" not in kinds, (
        f"log-backed steer must not re-emit after consumed window advances; "
        f"kinds: {kinds}"
    )
    assert after["offset"] == full_offset + 1


def test_reviewer_steer_surfaces_in_lens_researcher_stream(home: Path) -> None:
    """r5 fix #3 (r4 reasserted): a ``role_target='reviewer'`` steer
    surfaces in the stream of a researcher node whose id ends in
    ``_lens`` (e.g. ``code_impact_lens``), because ``_role_for_node``
    maps such researchers to ``reviewer``.

    The default seed has a steer targeted at ``AGENT_NODE`` (the
    implementer id, not the 'reviewer' role), so we inject a fresh
    reviewer-targeted row and verify the lens node sees it. A non-lens
    researcher does NOT see reviewer-targeted steers — its role falls
    through to the default.
    """
    _seed(home)

    # Insert the lens researcher node in the events table so the loader
    # picks it up. The default workflow only knows about ``implementer``
    # and ``verifier_node`` — we mirror the kickoff's recipe where
    # ``code_impact_lens`` is dispatched as a researcher.
    con = sqlite3.connect(home / "state.db")
    for kind, ts in (("node_start", T0 + 55), ("node_end", T0 + 58)):
        payload = {"node_id": "code_impact_lens", "node_type": "researcher",
                   "model_lane": "minimax_lens"}
        if kind == "node_end":
            payload["finish_reason"] = "done"
        con.execute("INSERT INTO run_events (event_id, run_id, event_type, "
                    "payload_json, created_at) VALUES (?,?,?,?,?)",
                    (f"ev-lens-{kind}", RUN, kind, json.dumps(payload), ts))
    expires_ms = int(time.time() * 1000) + 3600_000
    con.execute(
        "INSERT INTO operator_steering (run_id, role_target, severity, message, source, "
        "confidence, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (RUN, "reviewer", "info", "for the lens reviewer", "ide", 0.8,
         (T0 + 56) * 1000, expires_ms))
    con.commit()
    con.close()

    # Reload AFTER the insert so ``code_impact_lens`` is in ``run_obj.nodes``.
    run_obj = _load(home, RUN)
    assert any(n.id == "code_impact_lens" for n in run_obj.nodes), (
        f"loader did not surface lens researcher; nodes: "
        f"{[n.id for n in run_obj.nodes]}"
    )
    out = build_node(home, RUN, "code_impact_lens", view="stream")
    steers = [e["arg"] for e in out["entries"] if e["k"] == "steer"]
    assert any("for the lens reviewer" in s for s in steers), (
        f"lens researcher should see reviewer-targeted steers; got: "
        f"{[e for e in out['entries'] if e['k'] == 'steer']}"
    )

    # A non-lens researcher (id does NOT end in "_lens") falls through
    # to the default role → does NOT see reviewer-targeted steers.
    for kind, ts in (("node_start", T0 + 55), ("node_end", T0 + 58)):
        payload = {"node_id": "scout", "node_type": "researcher",
                   "model_lane": "minimax_lens"}
        if kind == "node_end":
            payload["finish_reason"] = "done"
        con = sqlite3.connect(home / "state.db")
        con.execute("INSERT INTO run_events (event_id, run_id, event_type, "
                    "payload_json, created_at) VALUES (?,?,?,?,?)",
                    (f"ev-scout-{kind}", RUN, kind, json.dumps(payload), ts))
        con.commit()
        con.close()
    scout_out = build_node(home, RUN, "scout", view="stream")
    scout_steers = [e["arg"] for e in scout_out["entries"] if e["k"] == "steer"]
    assert not any("for the lens reviewer" in s for s in scout_steers), (
        f"non-lens researcher should NOT see reviewer-targeted steers; got: "
        f"{scout_steers}"
    )
