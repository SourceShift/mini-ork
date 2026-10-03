"""Pure mapping: claude stream-json events → ACP update objects.

The orchestrator driver (``mini_ork.acp_orchestrator.harness``) streams
``claude``-CLI stream-json objects to a callback; this module is the pure
mapper that turns each block into zero-or-more ACP ``session_update`` payloads
the agent's ``session_update`` seam can forward verbatim. Keeping the mapper
pure (no I/O, no subprocess, no agent state) means the unit test for it is a
plain ``dict → object`` assertion — the agent's wire concerns are tested
separately.

Two responsibilities, both stateless:

1. ``map_event(event)``: turn a single ``{"type": "assistant"|"user", ...}``
   envelope into a list of ACP update objects. The agent then emits each in
   order.

2. ``extract_child_run(tool_name, tool_result_text)``: when the
   ``start_run`` MCP tool answers with a JSON blob carrying a ``run_id``,
   return the ``run_id`` so the agent can spawn a child-run follower.
   Non-JSON text, other tool names, or a missing ``run_id`` return ``None``
   — the orchestrator turn continues unchanged.

Wire contract (per kickoff Z9c-1):

* ``assistant.text`` block → ``AgentMessageChunk``
* ``assistant.thinking`` block → ``AgentThoughtChunk``
* ``assistant.tool_use`` block → ``ToolCallStart`` (in_progress, kind="read"
  for the orchestrator's read tools; "other" otherwise; raw_input carried)
* ``user.tool_result`` block → ``ToolCallProgress`` (completed, or failed when
  ``is_error``); text capped at 2_000 chars
* ``user.tool_result`` answering ``start_run`` → the mapper ALSO returns the
  extracted ``run_id`` via the ``extract_child_run`` helper, but does not
  itself emit a child-run projection — that is the agent's job, which keys
  child tool ids on ``"<run_id>:"`` to avoid collisions

This module never imports ``mini_ork.acp.agent``; one-way dependency:
``agent → orchestration``, never the reverse.
"""
from __future__ import annotations

import json
from typing import Any, Literal, Sequence, Union, cast

from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    ContentToolCallContent,
    FileEditToolCallContent,
    TerminalToolCallContent,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
)

# Pydantic's ToolCallProgress.content type is invariant in pyright when narrowed
# to a single-variant list; widen once at the alias so the call sites stay clean.
_ToolProgressContent = Sequence[
    Union[ContentToolCallContent, FileEditToolCallContent, TerminalToolCallContent]
]

__all__ = [
    "CHILD_RUN_TOOL",
    "extract_child_run",
    "map_event",
]


# Tools the orchestrator is allowed to use (mirrors harness._ALLOWED_TOOLS).
# A ``tool_use`` whose name is one of these renders as kind="read"; anything
# else (notably MCP ``mcp__mini-ork__*`` tools) renders as kind="other". The
# set is duplicated here so this module imports no orchestrator internals.
_READ_TOOL_NAMES = frozenset({"Read", "Grep", "Glob", "LS"})

# The MCP control tool whose ``tool_result`` carries a JSON ``run_id`` we must
# follow as a child run. Mirrors ``mini_ork.mcp_context.server._control_tools``.
CHILD_RUN_TOOL = "start_run"

# Per the kickoff: tool_result text is capped so a runaway tool dump cannot
# blow up a single ACP chunk. Keep parity with ``mini_ork.acp.live``.
_TOOL_RESULT_TEXT_CAP = 2000


def _tool_title(name: str) -> str:
    """Strip the MCP prefix and turn ``mcp__mini-ork__start_run`` into ``start_run``.

    The orchestrator's read tools keep their bare names; the MCP ``mini-ork``
    tools land under the ``mcp__mini-ork__`` prefix the agent renders on the
    wire. Anything else stays verbatim — the test suite verifies the exact
    shapes the kickoff mandates.
    """
    if name.startswith("mcp__mini-ork__"):
        return name[len("mcp__mini-ork__") :]
    return name


def _summarize_input(input_obj: Any) -> str:
    """Short single-line preview of a tool_use ``input`` payload.

    A path-spam ``input`` like ``{"path": "...very long..."}`` would otherwise
    blow up the chunk size. We render a compact JSON; if it overflows the
    ``_INPUT_PREVIEW_CAP``, the tail is dropped.
    """
    _INPUT_PREVIEW_CAP = 80
    try:
        rendered = json.dumps(input_obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = repr(input_obj)
    if len(rendered) > _INPUT_PREVIEW_CAP:
        rendered = rendered[:_INPUT_PREVIEW_CAP] + "…"
    return rendered


def _kind_for(name: str) -> Literal["read", "other"]:
    """Return ``"read"`` for the orchestrator's read tools, else ``"other"``."""
    return "read" if name in _READ_TOOL_NAMES else "other"


def _assistant_message_blocks(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the content blocks list out of a claude assistant envelope.

    The envelope shape is ``{"type": "assistant", "message": {"content":
    [{"type": "text|thinking|tool_use", ...}, ...]}}``. Defensive against
    missing or wrong-shaped fields so a malformed event yields ``[]`` rather
    than crashing the projection.
    """
    message = envelope.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _user_tool_result_blocks(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the ``tool_result`` blocks out of a claude user envelope.

    The envelope shape is ``{"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "...", "is_error": bool,
     "content": [{"type": "text", "text": "..."}]}, ...]}}``. The kickoff
    specifies one ``tool_result`` per user envelope; we tolerate multiple
    defensively (silently ignoring the extras).
    """
    message = envelope.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]


def map_event(event: dict[str, Any]) -> list[Any]:
    """Turn one stream-json envelope into a list of ACP update objects.

    Returns ``[]`` for unknown envelopes (``{"type": "system", ...}``, a
    claude ``stream_event`` delta, the final ``{"type": "result", ...}``,
    etc.) — the agent's caller emits nothing for those and the orchestrator
    turn continues. The four recognised cases match the four output flavours
    documented in the module docstring.
    """
    typ = str(event.get("type") or "")
    if typ == "assistant":
        updates: list[Any] = []
        for block in _assistant_message_blocks(event):
            bt = str(block.get("type") or "")
            if bt == "text":
                text = str(block.get("text") or "")
                if text:
                    updates.append(
                        AgentMessageChunk(
                            session_update="agent_message_chunk",
                            content=TextContentBlock(type="text", text=text),
                        )
                    )
            elif bt == "thinking":
                text = str(block.get("thinking") or "")
                if text:
                    updates.append(
                        AgentThoughtChunk(
                            session_update="agent_thought_chunk",
                            content=TextContentBlock(type="text", text=text),
                        )
                    )
            elif bt == "tool_use":
                name = str(block.get("name") or "tool")
                tool_use_id = str(block.get("id") or "")
                summary = _summarize_input(block.get("input"))
                raw_title = _tool_title(name)
                title = f"{raw_title}: {summary}" if summary else raw_title
                updates.append(
                    ToolCallStart(
                        session_update="tool_call",
                        tool_call_id=tool_use_id,
                        title=title,
                        status="in_progress",
                        kind=_kind_for(name),
                        raw_input=block.get("input"),
                    )
                )
        return updates
    if typ == "user":
        updates = []
        for block in _user_tool_result_blocks(event):
            tool_use_id = str(block.get("tool_use_id") or "")
            is_error = bool(block.get("is_error"))
            text = _tool_result_text(block)
            content: _ToolProgressContent | None = None
            if text:
                content = [
                    ContentToolCallContent(
                        type="content",
                        content=TextContentBlock(type="text", text=text),
                    )
                ]
            updates.append(
                ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=tool_use_id,
                    status="failed" if is_error else "completed",
                    content=cast(Any, content),
                )
            )
        return updates
    return []


def _tool_result_text(block: dict[str, Any]) -> str:
    """Concatenate the first text block of a tool_result's content list.

    Truncated at ``_TOOL_RESULT_TEXT_CAP`` characters so a chatty tool cannot
    push an unbounded chunk onto the wire.
    """
    inner = block.get("content")
    # Built-in tools (Read, Glob, …) answer with a plain string; MCP tools
    # answer with a list of content blocks.
    if isinstance(inner, str):
        inner = [{"type": "text", "text": inner}]
    if not isinstance(inner, list):
        return ""
    parts: list[str] = []
    for c in inner:
        if not isinstance(c, dict):
            continue
        text = c.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
            break  # first text block per the kickoff spec
    if not parts:
        return ""
    text = "".join(parts)
    if len(text) > _TOOL_RESULT_TEXT_CAP:
        text = text[:_TOOL_RESULT_TEXT_CAP] + "…"
    return text


def extract_child_run(tool_name: str, tool_result_text: str) -> str | None:
    """Return the ``run_id`` from a ``start_run`` tool_result, else ``None``.

    ``tool_name`` is the bare name (``start_run`` after MCP-prefix stripping).
    ``tool_result_text`` is the body of the first text block in the result.

    A non-JSON body, a different tool, or a JSON body with no ``run_id``
    field all return ``None`` — the orchestrator turn continues and the agent
    does not spawn a child-run follower. A valid ``run_id`` is also
    ``_is_safe_token``-checked (matching the Web control path) so a hostile
    body cannot inject a path-traversal id.
    """
    if tool_name != CHILD_RUN_TOOL:
        return None
    if not tool_result_text or not tool_result_text.strip():
        return None
    try:
        payload = json.loads(tool_result_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    # Defensive: a hostile body could carry ``"../../etc/passwd"``-shaped ids.
    # Defer the import to avoid a module-load cycle on mini_ork.web.control.
    from mini_ork.web.control import _is_safe_token

    if not _is_safe_token(run_id):
        return None
    return run_id