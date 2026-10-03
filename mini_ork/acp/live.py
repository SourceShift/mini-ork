"""Per-node live sidecar tailer + envelope normalizer for the ACP projection.

Slice Z3 of the Zed engineer surface: the dispatch path now writes
``<run_dir>/agent-<node>.live.jsonl`` for every in-a-run node (host too —
see ``mini_ork.dispatch.providers._attach_isolation``); this module is the
reader-side surface that turns each ``LiveWriter`` record into one or more
ACP-friendly events.

The filename ``agent-{node}.live.jsonl`` is the same one used by the web
``/api/v1/runs/{run_id}/nodes/{node}/live`` route (``mini_ork.web.routes.
node_live.LIVE_FILE_NAME``) and the SSE ``/stream`` route
(``mini_ork.web.routes.stream.LIVE_FILE_NAME``). Accepting the duplication
is intentional — refactoring three readers onto one helper is out of scope
for the Z3 slice, and the drift risk is bounded by a docstring cross-link
in each consumer.

Reader discipline mirrors ``mini_ork.web.routes.node_live.get_live``:

* holds a byte offset so each call returns only new bytes;
* a trailing partial line waits for the next call;
* truncation (offset > size) resets the offset to 0 rather than skipping
  into garbage;
* a missing file returns ``[]``;
* lines that do not parse as JSON objects are skipped — the on-disk format
  is always JSON, but a future plain-log fallback must not break the tailer.

The reader is NOT thread-safe; one ``LiveTail`` per ``(session, node)``.
The ACP agent constructs them lazily on first ``node_start`` and keeps them
in ``self._tails[session_id][node_id]`` for the lifetime of the session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Live-sidecar record schema (``mini_ork.dispatch.live_stream.LiveWriter``):
#     {"seq": int, "stream": "stdout"|"stderr"|"meta", "t": float,
#      "line": str, "partial"?: bool, "truncated"?: bool}
# `line` carries the raw underlying CLI stream — claude stream-json objects
# for the claude-CLI lane and codex JSONL envelopes for the codex lane —
# wrapped by the LiveWriter's append-and-flush record.

# Per-event text cap (kickoff §3): each emitted text/thought/tool/tool_output
# is capped so a runaway agent cannot push an unbounded chunk onto the ACP wire.
_TEXT_CAP = 2000

# Per-tool-output preview cap (kickoff §2 / claude `tool_result`, codex
# `command_execution.aggregated_output`): a CLI dump is rarely interesting
# past the last 400 chars; truncating here keeps each chunk bounded.
_TOOL_OUTPUT_CAP = 400

# Per-tool-use input compact preview (kickoff §2): the JSON-encoded input is
# truncated so a path-spam call doesn't blow up the wire chunk.
_TOOL_INPUT_CAP = 200

# Reader-side cap on bytes consumed per poll. Picked generously above the
# expected 1-2 KB of typical claude/codex output to amortize syscalls
# without holding the asyncio loop for too long.
_DEFAULT_MAX_BYTES = 256_000

_VALID_KINDS = frozenset({"text", "thought", "tool", "tool_output", "result"})


@dataclass
class LiveEvent:
    """One normalized event ready for ACP emission.

    ``kind`` ∈ ``{"text", "thought", "tool", "tool_output"}``. ``text`` is
    already capped at ``_TEXT_CAP`` and stripped; empty texts are dropped
    before reaching this shape (see ``normalize``).
    """

    kind: str
    text: str


class LiveTail:
    """Byte-offset tailer for one ``agent-<node>.live.jsonl`` sidecar.

    A new ``LiveTail(path)`` starts at offset 0. ``read_new()`` returns the
    complete new JSONL records (as parsed dicts) since the last call and
    advances ``self.offset`` past the consumed bytes. Partial trailing lines
    stay in the file for the next call.

    Truncation handling: if the on-disk size shrinks below ``self.offset``,
    ``self.offset`` is reset to 0 and the next read starts at the beginning.
    The caller does NOT see a ``truncated`` flag — it just gets the new bytes
    from the start, capped at ``max_bytes``.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.offset = 0

    def read_new(self, max_bytes: int = _DEFAULT_MAX_BYTES) -> list[dict]:
        """Parse complete new JSONL records since the last call.

        Returns ``[]`` when the file is missing, empty, or the only bytes
        available form a partial line. A truncation shrinks ``self.offset``
        to 0 silently; the caller observes the new bytes on the next call.
        """
        try:
            size = self.path.stat().st_size
        except OSError:
            self.offset = 0
            return []
        if self.offset > size:
            # The file shrank since the last call (LiveWriter's byte cap
            # rewinds by truncating back to zero bytes; any future cap+rewrite
            # would land here too). Reset and read from the beginning.
            self.offset = 0
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                data = fh.read(max_bytes)
        except OSError:
            return []
        if not data:
            return []
        # Consume only up to and including the LAST newline in the read
        # window. Any bytes after that last newline are a partial line and
        # wait for the next call. ``bytes.rfind`` is on bytes so a UTF-8
        # multi-byte char straddling the window would still produce a clean
        # split at the byte boundary we choose.
        last_nl = data.rfind(b"\n")
        if last_nl < 0:
            # No complete line in this read — wait for more data.
            return []
        consumed = data[: last_nl + 1]
        self.offset += len(consumed)
        text = consumed.decode("utf-8", errors="replace")
        out: list[dict] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # On-disk format is always JSON; a future plain-log fallback
                # would land here. Skip silently — do not crash the tailer.
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out


def normalize(record: dict) -> list[LiveEvent]:
    """Convert one ``LiveWriter`` record into zero or more ``LiveEvent``s.

    ``record`` is the outer ``{seq, stream, t, line, ...}`` envelope written
    by ``LiveWriter.write_line``; this function re-parses ``record["line"]``
    and walks the inner envelope to extract the chunks. Two CLI shapes are
    recognized:

    * claude stream-json envelopes (``{"type":"assistant"|"user", ...}``);
    * codex JSONL envelopes (``{"type":"item.completed", "item":{...}}``).

    A final ``result`` object becomes one ``result`` event (the caller shows it
    only when the node streamed no text). Anything else — claude
    ``stream_event`` deltas, ``system`` envelopes, future unknown shapes —
    yields ``[]``. Each returned
    ``LiveEvent`` has ``text`` capped at ``_TEXT_CAP`` and stripped; empties
    are dropped.
    """
    out: list[LiveEvent] = []
    if not isinstance(record, dict):
        return out
    line = record.get("line")
    if not isinstance(line, str) or not line.strip():
        return out
    try:
        inner = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return out
    if not isinstance(inner, dict):
        return out
    typ = str(inner.get("type") or "")
    if typ == "assistant":
        out.extend(_claude_assistant(inner))
    elif typ == "user":
        out.extend(_claude_user_tool_result(inner))
    elif typ == "item.completed":
        out.extend(_codex_item_completed(inner))
    elif typ == "result" or ("result" in inner and "session_id" in inner and "usage" in inner):
        # The final answer. A lane that does not stream (json output) writes only
        # this object at exit; callers show it only for a node that streamed no
        # text, so a streaming lane's answer is never shown twice.
        text = inner.get("result")
        if isinstance(text, str) and text.strip():
            out.append(LiveEvent(kind="result", text=text))
    return _cap_and_strip(out)


def _claude_assistant(envelope: dict) -> list[LiveEvent]:
    """Extract text / thinking / tool_use from a claude assistant message."""
    out: list[LiveEvent] = []
    message = envelope.get("message")
    if not isinstance(message, dict):
        return out
    content = message.get("content")
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict):
            continue
        bt = str(block.get("type") or "")
        if bt == "text":
            text = str(block.get("text") or "")
            if text:
                out.append(LiveEvent(kind="text", text=text))
        elif bt == "thinking":
            text = str(block.get("thinking") or "")
            if text:
                out.append(LiveEvent(kind="thought", text=text))
        elif bt == "tool_use":
            name = str(block.get("name") or "tool")
            payload = block.get("input")
            try:
                compact = json.dumps(payload, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                compact = repr(payload)
            if len(compact) > _TOOL_INPUT_CAP:
                compact = compact[:_TOOL_INPUT_CAP] + "…"
            out.append(LiveEvent(kind="tool", text=f"{name}: {compact}"))
    return out


def _claude_user_tool_result(envelope: dict) -> list[LiveEvent]:
    """Extract ``tool_result`` text from a claude user envelope.

    The user envelope shape is ``{"type":"user","message":{"content":[
    {"type":"tool_result","content":[{"type":"text","text":"..."}],
     "tool_use_id":"..."}]}}``. We pull the first text block; anything
    after the cap is dropped — the operator wants the gist, not the dump.
    """
    out: list[LiveEvent] = []
    message = envelope.get("message")
    if not isinstance(message, dict):
        return out
    blocks = message.get("content")
    if not isinstance(blocks, list):
        return out
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "") != "tool_result":
            continue
        result_content = block.get("content")
        if not isinstance(result_content, list):
            continue
        for c in result_content:
            if not isinstance(c, dict):
                continue
            text = c.get("text")
            if isinstance(text, str) and text:
                out.append(
                    LiveEvent(kind="tool_output", text=text[:_TOOL_OUTPUT_CAP])
                )
                break
        break  # one tool_result per envelope
    return out


def _codex_item_completed(envelope: dict) -> list[LiveEvent]:
    """Extract text / reasoning / command / file_change from codex items."""
    out: list[LiveEvent] = []
    item = envelope.get("item")
    if not isinstance(item, dict):
        return out
    it = str(item.get("type") or "")
    if it == "agent_message":
        text = str(item.get("text") or "")
        if text:
            out.append(LiveEvent(kind="text", text=text))
    elif it == "reasoning":
        text = str(item.get("text") or "")
        if text:
            out.append(LiveEvent(kind="thought", text=text))
    elif it == "command_execution":
        cmd = str(item.get("command") or "")
        out.append(LiveEvent(kind="tool", text=f"$ {cmd}"))
        agg = str(item.get("aggregated_output") or "")
        if agg:
            # codex's ``aggregated_output`` is the LAST ``max_wait_ms`` of
            # captured output — taking its tail matches operator intent.
            out.append(LiveEvent(kind="tool_output", text=agg[-_TOOL_OUTPUT_CAP:]))
    elif it == "file_change":
        changes = item.get("changes")
        if isinstance(changes, list):
            paths = [
                str(c.get("path") or "")
                for c in changes
                if isinstance(c, dict)
            ]
            paths = [p for p in paths if p]
            if paths:
                out.append(LiveEvent(kind="tool", text=f"edited {', '.join(paths)}"))
    return out


def _cap_and_strip(events: list[LiveEvent]) -> list[LiveEvent]:
    """Drop empties, strip, and cap each ``text`` at ``_TEXT_CAP``."""
    out: list[LiveEvent] = []
    for ev in events:
        if ev.kind not in _VALID_KINDS:
            continue
        text = (ev.text or "").strip()
        if not text:
            continue
        if len(text) > _TEXT_CAP:
            text = text[:_TEXT_CAP] + "…"
        out.append(LiveEvent(kind=ev.kind, text=text))
    return out


__all__ = ["LiveEvent", "LiveTail", "normalize"]