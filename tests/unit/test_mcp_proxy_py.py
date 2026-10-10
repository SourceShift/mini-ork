"""stdio ↔ streamable-HTTP bridge tests.

A real (in-process) HTTP server stands in for the remote MCP endpoint: a fake
``urlopen`` would pin the *shape* of the code rather than the wire behaviour the
bridge exists to get right — the SSE payload extraction, the ``Mcp-Session-Id``
echo, and the 202-with-no-body case that a notification produces.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mini_ork.dispatch.mcp_proxy import run_proxy


class _Handler(BaseHTTPRequestHandler):
    """An MCP streamable-HTTP endpoint with canned behaviour per JSON-RPC method."""

    received: list = []

    def do_POST(self):  # noqa: N802 - http.server's name
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        # Header names are case-insensitive on the wire, and urllib normalises
        # them while http.server preserves what arrived — so compare lowercased.
        type(self).received.append(
            {"message": payload, "headers": {k.lower(): v for k, v in self.headers.items()}}
        )
        method = payload.get("method")

        if method == "initialize":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "sess-42")
            self.end_headers()
            self.wfile.write(json.dumps(
                {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"ok": True}}
            ).encode())
        elif method == "notifications/initialized":
            # The spec's canonical "no body" reply.
            self.send_response(202)
            self.end_headers()
        elif method == "tools/list":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            body = (
                'event: message\n'
                'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n\n'
            )
            self.wfile.write(body.encode())
        elif method == "boom":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(
                {"jsonrpc": "2.0", "id": payload.get("id"),
                 "error": {"code": -32603, "message": "exploded"}}
            ).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def endpoint():
    """A live MCP endpoint; yields ``(url, received)``."""
    _Handler.received = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", _Handler.received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _Lines:
    """A minimal stdin stand-in: iterate lines, then stop."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        return iter(self._lines)


class _Capture:
    def __init__(self):
        self.lines: list[str] = []

    def write(self, text):
        self.lines.append(text)

    def flush(self):
        pass

    @property
    def text(self):
        return "".join(self.lines)


def test_proxy_posts_and_forwards_a_json_body(endpoint):
    url, received = endpoint
    out = _Capture()
    assert run_proxy(
        url, {}, stdin=_Lines(['{"jsonrpc":"2.0","id":1,"method":"initialize"}']),
        stdout=out,
    ) == 0
    assert json.loads(out.text.strip()) == {
        "jsonrpc": "2.0", "id": 1, "result": {"ok": True}
    }
    assert received[0]["headers"]["accept"] == "application/json, text/event-stream"


def test_proxy_extracts_an_sse_payload(endpoint):
    url, _ = endpoint
    out = _Capture()
    assert run_proxy(
        url, {}, stdin=_Lines(['{"jsonrpc":"2.0","id":2,"method":"tools/list"}']),
        stdout=out,
    ) == 0
    assert json.loads(out.text.strip())["result"] == {"tools": []}


def test_proxy_echoes_the_session_id(endpoint):
    url, received = endpoint
    out = _Capture()
    run_proxy(
        url, {},
        stdin=_Lines([
            '{"jsonrpc":"2.0","id":1,"method":"initialize"}',
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
        ]),
        stdout=out,
    )
    assert received[0]["headers"].get("mcp-session-id") is None
    assert received[1]["headers"]["mcp-session-id"] == "sess-42"


def test_a_notification_writes_nothing(endpoint):
    url, received = endpoint
    out = _Capture()
    assert run_proxy(
        url, {},
        stdin=_Lines(['{"jsonrpc":"2.0","method":"notifications/initialized"}']),
        stdout=out,
    ) == 0
    assert out.text == ""
    assert received[0]["message"]["method"] == "notifications/initialized"


def test_headers_are_sent_verbatim(endpoint):
    url, received = endpoint
    run_proxy(
        url, {"Authorization": "Bearer tok-123"},
        stdin=_Lines(['{"jsonrpc":"2.0","id":1,"method":"initialize"}']),
        stdout=_Capture(),
    )
    assert received[0]["headers"]["authorization"] == "Bearer tok-123"


def test_a_json_rpc_error_body_is_forwarded_not_fatal(endpoint):
    url, _ = endpoint
    out = _Capture()
    assert run_proxy(
        url, {}, stdin=_Lines(['{"jsonrpc":"2.0","id":3,"method":"boom"}']),
        stdout=out,
    ) == 0
    assert json.loads(out.text.strip())["error"]["message"] == "exploded"


def test_an_unreachable_endpoint_reports_and_stops(capsys):
    out = _Capture()
    code = run_proxy(
        "http://127.0.0.1:1/mcp", {},
        stdin=_Lines(['{"jsonrpc":"2.0","id":1,"method":"initialize"}']),
        stdout=out,
    )
    assert code == 2
    assert out.text == ""
    assert "cannot reach" in capsys.readouterr().err


def test_non_json_stdin_is_dropped_not_forwarded(endpoint, capsys):
    url, received = endpoint
    out = _Capture()
    assert run_proxy(url, {}, stdin=_Lines(["not json"]), stdout=out) == 0
    assert out.text == ""
    assert received == []
    assert "non-JSON" in capsys.readouterr().err
