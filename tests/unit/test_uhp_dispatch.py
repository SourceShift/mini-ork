"""Contracts for the ``uhp`` provider kind and a UHP harness lane.

* network-free unit tests drive ``mini_ork.dispatch.uhp.run`` against a real
  ``http.server.ThreadingHTTPServer`` bound to ``127.0.0.1:0`` — the chunked
  SSE read path is hard to fake cleanly with a mocked ``urlopen``, so we run
  a tiny in-process server whose handler emits scripted events;
* registry-level tests prove the builder, kind-sets, and preflight surface the
  lane correctly and that the existing dispatch flow is unchanged for non-UHP
  kinds.
"""
from __future__ import annotations

import io
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from mini_ork.dispatch import uhp as transport
from mini_ork.dispatch.providers import (
    lane_health,
    required_secret_envs,
    resolve_provider,
)


# ── Fake UHP server harness ────────────────────────────────────────────────


# Shared per-server state lives on the handler class so do_POST (which only
# gets `self.server`) can reach it without attribute-typed access to the
# ThreadingHTTPServer subclass.
_SCRIPTED_STATE: dict[int, dict[str, Any]] = {}


class _ScriptedHandler(BaseHTTPRequestHandler):
    """Handler that emits one scripted SSE response per request, in order."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — stdlib signature
        return  # silence stderr noise in tests

    def do_POST(self) -> None:  # noqa: N802 — stdlib signature
        # ThreadingHTTPServer has no public `id`, so use the listening socket's
        # fileno as a stable per-instance key.
        key = self.server.socket.fileno()  # type: ignore[attr-defined]
        state = _SCRIPTED_STATE[key]
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length else b""
        try:
            body: Any = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except json.JSONDecodeError:
            body = raw_body.decode("utf-8", "replace")
        state["requests"].append(
            {
                "path": self.path,
                "headers": {k: v for k, v in self.headers.items()},
                "body": body,
            }
        )
        scripts: list[_Script] = state["scripts"]
        if not scripts:
            self.send_error(500, "no script queued")
            return
        script = scripts.pop(0)
        self.send_response(script.status)
        if script.status >= 400:
            self.send_header("Content-Type", "application/json")
            payload = script.body or b'{"error": "test"}'
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for chunk in script.body_chunks:
            self.wfile.write(chunk)
        self.wfile.flush()


class _Script:
    __slots__ = ("status", "body_chunks", "body")

    def __init__(
        self,
        status: int = 200,
        body_chunks: list[bytes] | None = None,
        body: bytes | None = None,
    ) -> None:
        self.status = status
        self.body_chunks = body_chunks or []
        self.body = body


class _FakeUHPServer:
    """ThreadingHTTPServer bound to a free port; serves scripted SSE."""

    def __init__(self) -> None:
        # Bind to a free port on loopback only.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        sock.close()
        self._server = ThreadingHTTPServer(("127.0.0.1", port), _ScriptedHandler)
        self.port = port
        key = self._server.socket.fileno()
        _SCRIPTED_STATE[key] = {
            "scripts": [],
            "requests": [],
        }
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="fake-uhp"
        )
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def requests(self) -> list[dict[str, Any]]:
        key = self._server.socket.fileno()
        return _SCRIPTED_STATE[key]["requests"]

    def script(self, s: _Script) -> None:
        key = self._server.socket.fileno()
        _SCRIPTED_STATE[key]["scripts"].append(s)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        key = self._server.socket.fileno()
        _SCRIPTED_STATE.pop(key, None)


def _sse(events: list[tuple[str, dict[str, Any]]]) -> list[bytes]:
    """Encode a list of (event_type, data) into SSE byte chunks."""
    out: list[bytes] = []
    for event_type, data in events:
        out.append(f"event: {event_type}\n".encode("utf-8"))
        out.append(f"data: {json.dumps(data)}\n".encode("utf-8"))
        out.append(b"\n")
    return out


def _env(monkeypatch, server: _FakeUHPServer, **overrides: str) -> dict[str, str]:
    """Set the env a transport expects, return the dict for assertions."""
    env = {
        "MO_UHP_BASE_URL": server.url,
        "MO_UHP_ENV_KEY": "UHP_API_KEY",
        "UHP_API_KEY": overrides.get("api_key", "sk-test"),
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if "harness_id" in overrides:
        monkeypatch.setenv("MO_UHP_HARNESS_ID", overrides["harness_id"])
    return env


def _env_setup_only(monkeypatch) -> None:
    """Set the env a transport expects for tests that do not need a server."""
    monkeypatch.setenv("MO_UHP_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("MO_UHP_ENV_KEY", "UHP_API_KEY")
    monkeypatch.setenv("UHP_API_KEY", "sk-test")


# ── Transport-level tests ────────────────────────────────────────────────────


def test_transport_emits_request_shape_and_completed_text(
    monkeypatch, capsys, tmp_path
):
    server = _FakeUHPServer()
    try:
        server.script(
            _Script(
                status=200,
                body_chunks=_sse(
                    [
                        (
                            "response.created",
                            {"type": "response.created", "response": {"id": "resp_abc"}},
                        ),
                        (
                            "response.output_text.delta",
                            {"type": "response.output_text.delta", "delta": "Hello"},
                        ),
                        (
                            "response.output_text.delta",
                            {"type": "response.output_text.delta", "delta": " world"},
                        ),
                        (
                            "response.completed",
                            {
                                "type": "response.completed",
                                "response": {
                                    "id": "resp_abc",
                                    "output": [
                                        {
                                            "content": [
                                                {"text": "Hello world", "type": "output_text"}
                                            ]
                                        }
                                    ],
                                },
                            },
                        ),
                    ]
                ),
            )
        )
        session_file = tmp_path / "uhp.session"
        _env(monkeypatch, server)
        monkeypatch.setenv("MO_UHP_SESSION_FILE", str(session_file))
        monkeypatch.setenv("MO_UHP_ERR_LOG", str(tmp_path / "uhp.err.log"))
        monkeypatch.setattr("sys.stdin", io.StringIO("Say hi"))

        rc = transport.run(["--print", "--output-format", "text"])

        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == "Hello world\n"
        assert "[" not in captured.err  # no error line on success

        # Request shape assertions.
        assert len(server.requests) == 1
        req = server.requests[0]
        assert req["path"] == "/v1/responses"
        assert req["headers"]["Authorization"] == "Bearer sk-test"
        assert req["headers"]["Content-Type"] == "application/json"
        assert req["headers"]["Accept"] == "text/event-stream"
        body = req["body"]
        assert body["input"] == "Say hi"
        assert body["stream"] is True
        assert "previous_response_id" not in body  # first call, no session

        # Session sidecar written.
        assert session_file.read_text(encoding="utf-8").strip() == "resp_abc"
    finally:
        server.close()


def test_transport_interleaved_deltas_accumulate_in_order(monkeypatch, capsys):
    server = _FakeUHPServer()
    try:
        server.script(
            _Script(
                status=200,
                body_chunks=_sse(
                    [
                        ("response.created", {"type": "response.created", "response": {"id": "r1"}}),
                        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "a"}),
                        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "b"}),
                        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "c"}),
                        (
                            "response.completed",
                            {
                                "type": "response.completed",
                                "response": {"id": "r1", "output": [{"content": [{"text": "abc"}]}]},
                            },
                        ),
                    ]
                ),
            )
        )
        _env(monkeypatch, server)
        monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

        rc = transport.run([])

        assert rc == 0
        assert capsys.readouterr().out == "abc\n"
    finally:
        server.close()


def test_transport_sends_previous_response_id_when_session_exists(
    monkeypatch, capsys, tmp_path
):
    server = _FakeUHPServer()
    try:
        session_file = tmp_path / "uhp.session"
        session_file.write_text("resp_prev\n", encoding="utf-8")
        server.script(
            _Script(
                status=200,
                body_chunks=_sse(
                    [
                        ("response.created", {"type": "response.created", "response": {"id": "resp_next"}}),
                        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "OK"}),
                        (
                            "response.completed",
                            {
                                "type": "response.completed",
                                "response": {"id": "resp_next", "output": [{"content": [{"text": "OK"}]}]},
                            },
                        ),
                    ]
                ),
            )
        )
        _env(monkeypatch, server)
        monkeypatch.setenv("MO_UHP_SESSION_FILE", str(session_file))
        monkeypatch.setattr("sys.stdin", io.StringIO("continue"))

        rc = transport.run([])

        assert rc == 0
        assert capsys.readouterr().out == "OK\n"
        assert server.requests[0]["body"]["previous_response_id"] == "resp_prev"
        # Sidecar updates to the new id.
        assert session_file.read_text(encoding="utf-8").strip() == "resp_next"
    finally:
        server.close()


def test_transport_includes_metadata_harness_id(monkeypatch):
    server = _FakeUHPServer()
    try:
        server.script(
            _Script(
                status=200,
                body_chunks=_sse(
                    [
                        ("response.created", {"type": "response.created", "response": {"id": "r"}}),
                        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "x"}),
                        (
                            "response.completed",
                            {
                                "type": "response.completed",
                                "response": {"id": "r", "output": [{"content": [{"text": "x"}]}]},
                            },
                        ),
                    ]
                ),
            )
        )
        _env(monkeypatch, server, harness_id="hrouter-ce")
        monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

        rc = transport.run([])

        assert rc == 0
        assert server.requests[0]["body"]["metadata"] == {"harness_id": "hrouter-ce"}
    finally:
        server.close()


# ── Error taxonomy rc mapping ────────────────────────────────────────────────


def test_transport_missing_key_env_rc2(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MO_UHP_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("MO_UHP_ENV_KEY", "UHP_API_KEY")
    monkeypatch.delenv("UHP_API_KEY", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("hi"))
    err_log = tmp_path / "uhp.err.log"
    monkeypatch.setenv("MO_UHP_ERR_LOG", str(err_log))

    rc = transport.run([])

    assert rc == transport.RC_CONFIG
    err = capsys.readouterr().err
    assert "UHP_API_KEY" in err
    assert "config" in err  # prefix-tagged
    assert err_log.read_text(encoding="utf-8") == "[uhp] config: $UHP_API_KEY is unset — lane would die silently\n"


def test_transport_missing_base_url_rc2(monkeypatch, capsys):
    monkeypatch.delenv("MO_UHP_BASE_URL", raising=False)
    monkeypatch.setenv("MO_UHP_ENV_KEY", "UHP_API_KEY")
    monkeypatch.setenv("UHP_API_KEY", "sk-test")
    monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

    rc = transport.run([])

    assert rc == transport.RC_CONFIG
    assert "MO_UHP_BASE_URL" in capsys.readouterr().err


def test_transport_empty_prompt_rc2(monkeypatch, capsys):
    _env_setup_only(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    rc = transport.run([])

    assert rc == transport.RC_CONFIG
    assert "no prompt" in capsys.readouterr().err


def test_transport_4xx_maps_to_config_rc2(monkeypatch, capsys):
    server = _FakeUHPServer()
    try:
        server.script(_Script(status=400, body=b'{"error": "bad request"}'))
        _env(monkeypatch, server)
        monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

        rc = transport.run([])

        assert rc == transport.RC_CONFIG
        assert "config" in capsys.readouterr().err
    finally:
        server.close()


def test_transport_5xx_maps_to_harness_rc4(monkeypatch, capsys):
    server = _FakeUHPServer()
    try:
        server.script(_Script(status=503, body=b'{"error": "unavailable"}'))
        _env(monkeypatch, server)
        monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

        rc = transport.run([])

        assert rc == transport.RC_HARNESS
        assert "harness" in capsys.readouterr().err
    finally:
        server.close()


def test_transport_connection_refused_maps_to_server_rc3(monkeypatch, capsys):
    # Bind a socket to get a guaranteed-free port, then close it so connect fails.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setenv("MO_UHP_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("MO_UHP_ENV_KEY", "UHP_API_KEY")
    monkeypatch.setenv("UHP_API_KEY", "sk-test")
    monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

    rc = transport.run([])

    assert rc == transport.RC_SERVER
    assert "server" in capsys.readouterr().err


def test_transport_sse_error_event_maps_to_harness_rc4(monkeypatch, capsys):
    server = _FakeUHPServer()
    try:
        server.script(
            _Script(
                status=200,
                body_chunks=_sse(
                    [
                        ("response.created", {"type": "response.created", "response": {"id": "r"}}),
                        (
                            "error",
                            {
                                "type": "error",
                                "code": "harness_error",
                                "message": "tool failed",
                            },
                        ),
                    ]
                ),
            )
        )
        _env(monkeypatch, server)
        monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

        rc = transport.run([])

        assert rc == transport.RC_HARNESS
        assert "tool failed" in capsys.readouterr().err
    finally:
        server.close()


def test_transport_incomplete_stream_maps_to_config_rc2(monkeypatch, capsys):
    server = _FakeUHPServer()
    try:
        server.script(
            _Script(
                status=200,
                body_chunks=_sse(
                    [
                        ("response.created", {"type": "response.created", "response": {"id": "r"}}),
                        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "partial"}),
                        # No response.completed — premature end of stream.
                    ]
                ),
            )
        )
        _env(monkeypatch, server)
        monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

        rc = transport.run([])

        assert rc == transport.RC_CONFIG
        assert "incomplete" in capsys.readouterr().err
    finally:
        server.close()


# ── Registry-level tests ────────────────────────────────────────────────────


def _clear_registry_overrides(monkeypatch) -> None:
    monkeypatch.delenv("MINI_ORK_PROVIDERS", raising=False)
    # Bypass the local .mini-ork/config/providers.yaml shadow so only the
    # explicit tmp_path candidate resolves (mirrors test_providers_registry).
    monkeypatch.setenv("MINI_ORK_HOME", "/nonexistent-miniork-home-for-tests")


def _write_registry(root: Path, providers: dict) -> Path:
    path = root / "config" / "providers.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    import yaml as _yaml
    path.write_text(_yaml.safe_dump({"providers": providers}), encoding="utf-8")
    return path


def test_uhp_kind_resolves_to_uhp_transport(tmp_path, monkeypatch):
    _clear_registry_overrides(monkeypatch)
    monkeypatch.setenv("UHP_API_KEY", "sk-test")
    _write_registry(
        tmp_path,
        {
            "uhp_local": {
                "kind": "uhp",
                "base_url": "http://localhost:8080",
                "api_key_env": "UHP_API_KEY",
            }
        },
    )

    spec = resolve_provider("uhp_local", tmp_path)

    assert spec.command[0] == sys.executable
    assert spec.command[1].endswith("uhp.py")
    assert spec.env["MO_UHP_BASE_URL"] == "http://localhost:8080"
    assert spec.env["MO_UHP_ENV_KEY"] == "UHP_API_KEY"
    assert spec.env["UHP_API_KEY"] == "sk-test"
    assert required_secret_envs("uhp_local", tmp_path) == ("UHP_API_KEY",)


def test_uhp_kind_requires_base_url(tmp_path, monkeypatch):
    _clear_registry_overrides(monkeypatch)
    monkeypatch.setenv("UHP_API_KEY", "sk-test")
    _write_registry(
        tmp_path,
        {"uhp_bad": {"kind": "uhp", "api_key_env": "UHP_API_KEY"}},
    )

    with pytest.raises(ValueError, match="base_url"):
        resolve_provider("uhp_bad", tmp_path)


def test_uhp_kind_requires_api_key_env(tmp_path, monkeypatch):
    _clear_registry_overrides(monkeypatch)
    _write_registry(
        tmp_path,
        {"uhp_bad": {"kind": "uhp", "base_url": "http://localhost:8080"}},
    )

    with pytest.raises(ValueError, match="api_key_env"):
        resolve_provider("uhp_bad", tmp_path)


def test_lane_health_ok_when_key_present(tmp_path, monkeypatch):
    _clear_registry_overrides(monkeypatch)
    monkeypatch.setenv("UHP_API_KEY", "sk-test")
    _write_registry(
        tmp_path,
        {
            "uhp_local": {
                "kind": "uhp",
                "base_url": "http://localhost:8080",
                "api_key_env": "UHP_API_KEY",
            }
        },
    )

    health = lane_health("uhp_local", tmp_path)

    assert health.ok is True
    assert health.reason == "ok"


def test_lane_health_missing_key_returns_unhealthy(tmp_path, monkeypatch):
    _clear_registry_overrides(monkeypatch)
    monkeypatch.delenv("UHP_API_KEY", raising=False)
    _write_registry(
        tmp_path,
        {
            "uhp_local": {
                "kind": "uhp",
                "base_url": "http://localhost:8080",
                "api_key_env": "UHP_API_KEY",
            }
        },
    )

    health = lane_health("uhp_local", tmp_path)

    assert health.ok is False
    assert "UHP_API_KEY" in health.reason
