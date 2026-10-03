"""Hermetic tests for the orchestrator stream-json mapper.

The mapper at ``mini_ork.acp.orchestration`` is pure (no I/O, no agent
state) so the assertions here are plain ``dict → object`` shape checks
against canned stream-json envelopes. The agent's wire concerns are
covered by ``test_acp_agent_py.py``; this module verifies that each
``{"type":"assistant"|"user",...}`` envelope the orchestrator's claude CLI
emits produces the right ACP update set.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from acp.schema import (  # noqa: E402
    AgentMessageChunk,
    AgentThoughtChunk,
    ContentToolCallContent,
    ToolCallProgress,
    ToolCallStart,
)

from mini_ork.acp import orchestration  # noqa: E402


def _assistant_envelope(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"content": content}}


def _user_tool_result(
    tool_use_id: str, text: str, *, is_error: bool = False
) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": is_error,
                    "content": [{"type": "text", "text": text}],
                }
            ]
        },
    }


# ── map_event: assistant blocks ────────────────────────────────────────────


def test_assistant_text_block_maps_to_agent_message_chunk():
    updates = orchestration.map_event(
        _assistant_envelope([{"type": "text", "text": "hello"}])
    )
    assert len(updates) == 1
    chunk = updates[0]
    assert isinstance(chunk, AgentMessageChunk)
    assert chunk.session_update == "agent_message_chunk"
    assert chunk.content.text == "hello"


def test_assistant_thinking_block_maps_to_agent_thought_chunk():
    updates = orchestration.map_event(
        _assistant_envelope([{"type": "thinking", "thinking": "pondering"}])
    )
    assert len(updates) == 1
    thought = updates[0]
    assert isinstance(thought, AgentThoughtChunk)
    assert thought.session_update == "agent_thought_chunk"
    assert thought.content.text == "pondering"


def test_assistant_tool_use_block_maps_to_tool_call_start_with_read_kind():
    updates = orchestration.map_event(
        _assistant_envelope(
            [{"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"path": "/x"}}]
        )
    )
    assert len(updates) == 1
    start = updates[0]
    assert isinstance(start, ToolCallStart)
    assert start.tool_call_id == "tool-1"
    assert start.status == "in_progress"
    assert start.kind == "read"
    # Title is "name: <short input>" so a single-line chunk doesn't blow up.
    assert start.title.startswith("Read: ")
    assert "/x" in start.title


def test_assistant_tool_use_strips_mcp_prefix_for_title():
    updates = orchestration.map_event(
        _assistant_envelope(
            [
                {
                    "type": "tool_use",
                    "id": "tool-2",
                    "name": "mcp__mini-ork__list_runs",
                    "input": {},
                }
            ]
        )
    )
    start = updates[0]
    assert isinstance(start, ToolCallStart)
    assert start.kind == "other"
    assert start.title.startswith("list_runs:")


def test_assistant_tool_use_long_input_is_truncated():
    updates = orchestration.map_event(
        _assistant_envelope(
            [
                {
                    "type": "tool_use",
                    "id": "tool-3",
                    "name": "Read",
                    "input": {"path": "/" + ("y" * 500)},
                }
            ]
        )
    )
    start = updates[0]
    assert isinstance(start, ToolCallStart)
    # Truncation marker `…` indicates the cap fired.
    assert "…" in start.title


def test_assistant_multiple_blocks_emit_in_order():
    updates = orchestration.map_event(
        _assistant_envelope(
            [
                {"type": "text", "text": "first"},
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "second"},
            ]
        )
    )
    assert [type(u).__name__ for u in updates] == [
        "AgentMessageChunk",
        "AgentThoughtChunk",
        "AgentMessageChunk",
    ]
    assert updates[0].content.text == "first"
    assert updates[1].content.text == "hmm"
    assert updates[2].content.text == "second"


# ── map_event: user tool_result blocks ─────────────────────────────────────


def test_user_tool_result_maps_to_completed_progress():
    updates = orchestration.map_event(
        _user_tool_result("tool-1", "output body")
    )
    assert len(updates) == 1
    prog = updates[0]
    assert isinstance(prog, ToolCallProgress)
    assert prog.session_update == "tool_call_update"
    assert prog.tool_call_id == "tool-1"
    assert prog.status == "completed"
    assert prog.content is not None
    block = prog.content[0]
    assert isinstance(block, ContentToolCallContent)
    assert block.content.text == "output body"


def test_builtin_tool_result_with_plain_string_content_keeps_its_text():
    # claude answers Read/Glob with ``content: "<text>"``, not a block list.
    updates = orchestration.map_event(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool-1", "content": "CHANGELOG.md"}
                ]
            },
        }
    )
    prog = updates[0]
    assert isinstance(prog, ToolCallProgress)
    assert prog.content is not None
    assert prog.content[0].content.text == "CHANGELOG.md"


def test_user_tool_result_with_is_error_maps_to_failed():
    updates = orchestration.map_event(
        _user_tool_result("tool-1", "boom", is_error=True)
    )
    prog = updates[0]
    assert isinstance(prog, ToolCallProgress)
    assert prog.status == "failed"


def test_user_tool_result_long_text_is_capped():
    long_text = "x" * 5000
    updates = orchestration.map_event(_user_tool_result("tool-1", long_text))
    prog = updates[0]
    assert isinstance(prog, ToolCallProgress)
    assert prog.content is not None
    rendered = prog.content[0].content.text
    # Capped at 2000 + trailing marker.
    assert len(rendered) <= 2001
    assert rendered.endswith("…")


def test_user_tool_result_missing_text_yields_empty_content():
    updates = orchestration.map_event(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": [{"type": "image", "data": "x"}],
                    }
                ]
            },
        }
    )
    prog = updates[0]
    assert isinstance(prog, ToolCallProgress)
    assert prog.content is None


# ── map_event: unknown envelopes ──────────────────────────────────────────


@pytest.mark.parametrize(
    "envelope",
    [
        {"type": "system", "subtype": "init"},
        {"type": "result", "session_id": "s1", "result": "all done"},
        {"type": "stream_event", "event": {"delta": {"text": "x"}}},
        {},
    ],
)
def test_unknown_envelopes_yield_empty_update_list(envelope):
    assert orchestration.map_event(envelope) == []


# ── extract_child_run ─────────────────────────────────────────────────────


def test_extract_child_run_returns_id_from_start_run_json():
    text = json.dumps({"run_id": "run-1-abc123"})
    assert (
        orchestration.extract_child_run(
            orchestration.CHILD_RUN_TOOL, text
        )
        == "run-1-abc123"
    )


def test_extract_child_run_returns_none_for_other_tools():
    text = json.dumps({"run_id": "run-1-abc123"})
    assert orchestration.extract_child_run("Read", text) is None
    assert orchestration.extract_child_run("list_runs", text) is None


def test_extract_child_run_returns_none_for_non_json_text():
    assert (
        orchestration.extract_child_run(
            orchestration.CHILD_RUN_TOOL, "this is not JSON"
        )
        is None
    )


def test_extract_child_run_returns_none_for_json_without_run_id():
    assert (
        orchestration.extract_child_run(
            orchestration.CHILD_RUN_TOOL, json.dumps({"status": "ok"})
        )
        is None
    )


def test_extract_child_run_returns_none_for_empty_text():
    assert orchestration.extract_child_run(orchestration.CHILD_RUN_TOOL, "") is None
    assert orchestration.extract_child_run(orchestration.CHILD_RUN_TOOL, "   ") is None


def test_extract_child_run_rejects_unsafe_token():
    text = json.dumps({"run_id": "../etc/passwd"})
    assert (
        orchestration.extract_child_run(orchestration.CHILD_RUN_TOOL, text)
        is None
    )


def test_extract_child_run_rejects_non_string_run_id():
    text = json.dumps({"run_id": 123})
    assert (
        orchestration.extract_child_run(orchestration.CHILD_RUN_TOOL, text)
        is None
    )