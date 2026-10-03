"""Hermetic tests for ``mini_ork.acp.live`` (Z3 reader side).

The reader is the tail the ACP projection uses to push per-node agent
output into the Zed thread. Two surfaces are under test:

1. ``LiveTail`` — byte-offset JSONL tailer. Mirrors the discipline of
   ``mini_ork.web.routes.node_live.get_live``: partial lines wait for the
   next call, truncation resets the offset to 0, a missing file returns
   ``[]``, and non-JSON lines are skipped (the on-disk format is always
   JSON, but a future plain-log fallback must not break the tailer).

2. ``normalize`` — envelope walker that turns one ``LiveWriter`` record
   into zero or more ``LiveEvent``s. Two CLI shapes are exercised:
   claude stream-json (``{"type":"assistant"|"user", ...}``) and codex
   JSONL (``{"type":"item.completed","item":{...}}``). Every text is
   capped at 2000 chars; tool inputs are compacted to 200 chars; tool
   outputs to 400 chars.

The four scenarios from the kickoff — partial-line, missing file,
truncation reset, non-JSON skip — map to four ``LiveTail`` tests here;
the claude / codex envelope matrix plus the unknown-type and cap
assertions cover ``normalize``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.acp.live import LiveEvent, LiveTail, normalize  # noqa: E402


def _record(line: str, *, seq: int = 0, stream: str = "stdout") -> dict:
    """Build a ``LiveWriter`` envelope around ``line``."""
    return {"seq": seq, "stream": stream, "t": 0.0, "line": line}


# ── LiveTail ───────────────────────────────────────────────────────────────


def test_live_tail_missing_file_returns_empty_and_keeps_offset_zero(tmp_path):
    tail = LiveTail(tmp_path / "absent.live.jsonl")
    assert tail.read_new() == []
    assert tail.offset == 0


def test_live_tail_partial_line_waits_for_more_bytes(tmp_path):
    """A trailing fragment without a newline must NOT be emitted; the
    offset must NOT advance past it. The next call, with the rest of the
    line, completes the parse."""
    path = tmp_path / "agent-impl.live.jsonl"
    tail = LiveTail(path)
    # No trailing newline — the last record's content is truncated mid-line.
    path.write_bytes(b'{"seq":0,"stream":"stdout","t":0.0,"line":"hello')
    assert tail.read_new() == [], "partial trailing line must wait"
    assert tail.offset == 0, "no complete line consumed yet"
    # Append the rest of the line and a newline; the next call sees both.
    with path.open("ab") as fh:
        fh.write(b' world"}\n')
    out = tail.read_new()
    assert [r["line"] for r in out] == ["hello world"]
    assert tail.offset == path.stat().st_size


def test_live_tail_consumes_complete_lines_and_advances_offset(tmp_path):
    path = tmp_path / "agent-impl.live.jsonl"
    path.write_text(
        _dumps(_record("hello", seq=0))
        + "\n"
        + _dumps(_record("world", seq=1))
        + "\n"
    )
    tail = LiveTail(path)
    assert [r["line"] for r in tail.read_new()] == ["hello", "world"]
    assert tail.offset == path.stat().st_size
    # Second call: nothing new.
    assert tail.read_new() == []
    assert tail.offset == path.stat().st_size


def test_live_tail_truncation_resets_offset_to_zero(tmp_path):
    """A file that shrunk below our offset is treated as truncated; the
    offset rewinds to 0 and the next read sees the new content."""
    path = tmp_path / "agent-impl.live.jsonl"
    # First record: longer content so the second write is unambiguously smaller.
    path.write_text(_dumps(_record("first-message-of-the-run", seq=0)) + "\n")
    tail = LiveTail(path)
    assert len(tail.read_new()) == 1
    consumed_offset = tail.offset
    assert consumed_offset > 0
    # Simulate cap+rewrite: smaller than offset.
    path.write_text(_dumps(_record("rewritten", seq=0)) + "\n")
    assert path.stat().st_size < consumed_offset
    out = tail.read_new()
    assert [r["line"] for r in out] == ["rewritten"]
    assert tail.offset == path.stat().st_size


def test_live_tail_skips_non_json_lines(tmp_path):
    path = tmp_path / "agent-impl.live.jsonl"
    path.write_text(
        "not json at all\n"
        + _dumps(_record("valid", seq=0))
        + "\n"
        + "{still not json\n"
        + _dumps(_record("also-valid", seq=1))
        + "\n"
    )
    tail = LiveTail(path)
    out = tail.read_new()
    assert [r["line"] for r in out] == ["valid", "also-valid"]


# ── normalize (claude envelopes) ────────────────────────────────────────────


def test_normalize_claude_assistant_text_becomes_text_event():
    rec = _record(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "hello world"}]},
    }))
    out = normalize(rec)
    assert out == [LiveEvent(kind="text", text="hello world")]


def test_normalize_claude_thinking_becomes_thought_event():
    rec = _record(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": "let me reason"}]},
    }))
    out = normalize(rec)
    assert out == [LiveEvent(kind="thought", text="let me reason")]


def test_normalize_claude_tool_use_compacts_input_to_200_chars():
    big = {"path": "/" + ("a" * 500)}
    rec = _record(json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use",
                                 "name": "Read",
                                 "input": big}]},
    }))
    out = normalize(rec)
    assert len(out) == 1
    assert out[0].kind == "tool"
    assert out[0].text.startswith("Read: {")
    assert len(out[0].text) <= 200 + 8  # 200 chars + "Read: {…"


def test_normalize_claude_tool_result_becomes_tool_output_capped_at_400():
    rec = _record(json.dumps({
        "type": "user",
        "message": {"content": [{
            "type": "tool_result",
            "content": [{"type": "text", "text": "x" * 1000}],
        }]},
    }))
    out = normalize(rec)
    assert len(out) == 1
    assert out[0].kind == "tool_output"
    assert len(out[0].text) == 400


# ── normalize (codex envelopes) ────────────────────────────────────────────


def test_normalize_codex_agent_message_becomes_text():
    rec = _record(json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "codex says hi"},
    }))
    out = normalize(rec)
    assert out == [LiveEvent(kind="text", text="codex says hi")]


def test_normalize_codex_reasoning_becomes_thought():
    rec = _record(json.dumps({
        "type": "item.completed",
        "item": {"type": "reasoning", "text": "planning"},
    }))
    out = normalize(rec)
    assert out == [LiveEvent(kind="thought", text="planning")]


def test_normalize_codex_command_execution_emits_tool_and_output():
    rec = _record(json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "ls",
            "aggregated_output": "alpha\nbeta\n" + ("z" * 1000),
        },
    }))
    out = normalize(rec)
    kinds = [e.kind for e in out]
    assert kinds == ["tool", "tool_output"]
    assert out[0].text == "$ ls"
    assert len(out[1].text) == 400
    # The output is the TAIL of the aggregated text (last 400 chars).
    assert out[1].text.endswith("zzz")


def test_normalize_codex_file_change_emits_tool_with_comma_joined_paths():
    rec = _record(json.dumps({
        "type": "item.completed",
        "item": {
            "type": "file_change",
            "changes": [
                {"path": "/a/foo.py"},
                {"path": "/a/bar.py"},
            ],
        },
    }))
    out = normalize(rec)
    assert out == [LiveEvent(kind="tool", text="edited /a/foo.py, /a/bar.py")]


# ── normalize (unknown / caps / edge cases) ─────────────────────────────────


def test_normalize_unknown_envelope_yields_empty():
    for typ in ("stream_event", "system", "result"):
        rec = _record(json.dumps({"type": typ, "anything": 1}))
        assert normalize(rec) == [], typ
    # Outer envelope that isn't a dict.
    assert normalize({"seq": 0, "line": "[1, 2, 3]"}) == []
    # Inner parse failure.
    assert normalize({"seq": 0, "line": "{not valid"}) == []
    # Empty / non-string line.
    assert normalize({"seq": 0, "line": ""}) == []
    assert normalize({"seq": 0}) == []


def test_normalize_drops_empty_texts():
    rec = _record(json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": ""},
            {"type": "text", "text": "   "},
            {"type": "text", "text": "kept"},
        ]},
    }))
    out = normalize(rec)
    assert out == [LiveEvent(kind="text", text="kept")]


def test_normalize_caps_each_text_at_2000_chars():
    rec = _record(json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "x" * 5000},
        ]},
    }))
    out = normalize(rec)
    assert len(out) == 1
    # 2000 chars + the trailing ellipsis.
    assert len(out[0].text) == 2001
    assert out[0].text.endswith("…")


# ── helpers ─────────────────────────────────────────────────────────────────


def _dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)

def test_normalize_turns_a_final_result_object_into_a_result_event():
    import json as _json

    from mini_ork.acp.live import normalize

    line = _json.dumps({"type": "result", "result": "done: edited CHANGELOG.md",
                        "session_id": "s", "usage": {}})
    evs = normalize({"seq": 0, "stream": "stdout", "t": 0.1, "line": line})
    assert [(e.kind, e.text) for e in evs] == [("result", "done: edited CHANGELOG.md")]
