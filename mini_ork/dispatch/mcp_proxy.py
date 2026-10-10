"""stdio ↔ streamable-HTTP bridge for a URL-based MCP server.

Zed spawns a *process* for a context server; a registry entry may be an HTTP
endpoint instead. Without a bridge the only way to give an editor such a server
is to paste its URL and its ``Authorization`` header into ``settings.json``,
which is exactly the thing this whole path exists to avoid — the header holds
the token.

So the entry names ``mini-ork mcp proxy <name>``; this module speaks stdio on
one side and streamable HTTP on the other, with the headers resolved from the
vault at spawn time and never written down.

Transport, per the MCP streamable-HTTP specification:

* one JSON-RPC message per line on stdio, in both directions;
* ``POST`` to the endpoint with ``Accept: application/json,
  text/event-stream``; the reply is either one JSON body or an SSE stream
  whose ``data:`` payloads are the messages;
* a ``Mcp-Session-Id`` returned by ``initialize`` is echoed on every later
  request, which is how a server keeps a session across stateless HTTP calls;
* a notification (no ``id``) may be answered ``202 Accepted`` with no body,
  and nothing is written back.

Deliberately a subset: request/response, which is what tool calls need. A
server that pushes unsolicited messages between calls will have them delivered
only while a call is in flight.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import IO, Any

#: Long enough for a slow tool call, short enough that a wedged endpoint does
#: not hang the editor forever.
DEFAULT_TIMEOUT = 120.0


class ProxyError(RuntimeError):
    """The bridge cannot continue; the message is safe to show the user."""


def _parse_sse(body: bytes) -> list[str]:
    """The JSON payloads of an SSE response, one per ``data:`` event."""
    messages: list[str] = []
    pending: list[str] = []
    for raw_line in body.decode("utf-8", errors="replace").splitlines():
        if raw_line.startswith("data:"):
            pending.append(raw_line[len("data:"):].lstrip())
        elif not raw_line.strip() and pending:
            messages.append("\n".join(pending))
            pending = []
    if pending:
        messages.append("\n".join(pending))
    return [message for message in messages if message.strip()]


def _write_message(stream: IO[str], text: str) -> None:
    stream.write(text.rstrip("\n") + "\n")
    stream.flush()


def _post(
    url: str,
    headers: Mapping[str, str],
    payload: str,
    session_id: str,
    timeout: float,
) -> tuple[list[str], str]:
    """POST one message. Returns ``(messages_to_forward, session_id)``."""
    request = urllib.request.Request(
        url, data=payload.encode("utf-8"), method="POST"
    )
    for name, value in headers.items():
        request.add_header(name, value)
    # Set after the caller's headers so an operator cannot accidentally make the
    # endpoint negotiate a content type we cannot parse.
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json, text/event-stream")
    if session_id:
        request.add_header("Mcp-Session-Id", session_id)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            session_id = response.headers.get("Mcp-Session-Id") or session_id
            content_type = (response.headers.get("Content-Type") or "").lower()
            body = response.read()
    except urllib.error.HTTPError as exc:
        # An MCP server reports a JSON-RPC error as an HTTP error body. That is
        # a message for the client, not a transport failure, so forward it and
        # keep the bridge up.
        body = exc.read() if hasattr(exc, "read") else b""
        forwarded = _parse_sse(body) if body else []
        if not forwarded and body:
            forwarded = [body.decode("utf-8", errors="replace").strip()]
        if forwarded and all(_is_json(message) for message in forwarded):
            return forwarded, session_id
        raise ProxyError(f"{url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProxyError(f"cannot reach {url}: {exc}") from exc

    if not body:
        return [], session_id
    if "text/event-stream" in content_type:
        return _parse_sse(body), session_id
    text = body.decode("utf-8", errors="replace").strip()
    return ([text] if text else []), session_id


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def run_proxy(
    url: str,
    headers: Mapping[str, str],
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> int:
    """Bridge stdio to ``url`` until stdin closes. Returns a process exit code."""
    source = sys.stdin if stdin is None else stdin
    sink = sys.stdout if stdout is None else stdout
    session_id = ""

    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message: Any = json.loads(line)
        except ValueError:
            sys.stderr.write("[mcp proxy] dropping a non-JSON line from stdin\n")
            continue
        if not isinstance(message, dict):
            sys.stderr.write("[mcp proxy] dropping a non-object message from stdin\n")
            continue
        try:
            forwarded, session_id = _post(url, headers, line, session_id, timeout)
        except ProxyError as exc:
            sys.stderr.write(f"[mcp proxy] {exc}\n")
            return 2
        for text in forwarded:
            _write_message(sink, text)
    return 0
