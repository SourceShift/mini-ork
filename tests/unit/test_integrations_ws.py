"""Tests for ``mini_ork.integrations._ws`` — the RFC 6455 client, offline.

No network and no TLS: every test drives the client over a
:func:`socket.socketpair`, which is a real connected socket, so the handshake and
the frame engine run their real code paths against a fake server that speaks
just enough RFC 6455. That is the seam the module is built around (see its
``connect(..., sock=...)``), and it is also what a Discord transport needs —
"drive a fake ``_ws`` connection" is the same trick.

The fake server side is written here, byte by byte, rather than reached for out
of the module under test: a test that parsed the client's frames with the
client's own reader would agree with itself no matter what either did. The one
number both sides share is the RFC's, and
:func:`test_accept_key_matches_the_rfc_example` pins it to the example §1.3 of
RFC 6455 publishes.

What is pinned:

* the opening handshake sends a well-formed upgrade request and *accepts* a
  correct ``Sec-WebSocket-Accept``, case- and order-insensitively;
* it *refuses* a wrong accept, and a status that is not ``101``;
* a client's frames are masked — an unmasked client send is dropped silently by
  every gateway this module exists to talk to;
* the read path answers a ping with a pong carrying the same payload, survives
  a control frame arriving *between* a message's fragments, and reassembles the
  fragments into one message;
* a close frame is surfaced as :class:`WebSocketClosed` with the peer's code and
  reason, a peer that simply vanishes is the same error rather than a hang, and
  a masked or reserved-bit server frame is refused as the protocol violation it
  is;
* the URL parse refuses anything that is not ``wss://`` *before* a socket is
  opened.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import struct
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.integrations import _ws  # noqa: E402

#: RFC 6455 §1.3's own handshake nonce, and the accept it must be answered with.
RFC_KEY = "dGhlIHNhbXBsZSBub25jZQ=="
RFC_ACCEPT = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

#: The GUID is restated here rather than imported: it is the RFC's constant, and
#: a test that took it from the module could not catch the module getting it wrong.
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

READ_TIMEOUT = 5.0


# -- the fake server side --------------------------------------------------


def _pair() -> tuple[socket.socket, socket.socket]:
    """A connected pair, both ends time-boxed so a bug fails instead of hanging."""
    client, server = socket.socketpair()
    client.settimeout(READ_TIMEOUT)
    server.settimeout(READ_TIMEOUT)
    return client, server


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        assert chunk, "the client closed mid-frame"
        buf += chunk
    return buf


def _read_head(sock: socket.socket) -> str:
    """The client's request head, as text — everything up to the blank line."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        assert chunk, "the client closed before finishing its handshake request"
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    assert rest == b"", "unexpected bytes after the request head"
    return head.decode("latin-1")


def _header(head: str, name: str) -> str | None:
    for line in head.split("\r\n")[1:]:
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == name.lower():
            return value.strip()
    return None


def _accept_for(key: str) -> str:
    """RFC 6455 §4.2.2, computed here so the module's answer is not its own witness."""
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def _respond(
    sock: socket.socket,
    key: str,
    *,
    accept: str | None = None,
    status: str = "101 Switching Protocols",
    upgrade: str = "websocket",
    connection: str = "keep-alive, Upgrade",
) -> None:
    """Write a handshake answer whose header names are lower-case and out of order.

    Deliberately not the casing or the ordering RFC 6455's example uses: a
    compliant client must read header names case-insensitively and in any order,
    and this is what proves it does.
    """
    lines = [
        f"HTTP/1.1 {status}",
        f"upgrade: {upgrade}",
        f"connection: {connection}",
        f"sec-websocket-accept: {accept if accept is not None else _accept_for(key)}",
    ]
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))


def _in_thread(fn: Callable[[], Any]) -> Callable[[], Any]:
    """Run ``fn`` on another thread; the returned callable joins and re-raises.

    Needed for the tests that reproduce a real handshake: over a ``socketpair``
    both ends are in one process, so a client that blocks reading its answer can
    only be answered by a thread. What the thread raises is re-raised here — a
    swallowed assertion would be exactly the false green this file is written to
    avoid.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — surfaced by the join, not swallowed
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    def join() -> Any:
        thread.join(READ_TIMEOUT)
        assert not thread.is_alive(), "the fake server side never finished"
        if "error" in box:
            raise box["error"]
        return box.get("value")

    return join


def _read_frame(sock: socket.socket) -> dict[str, Any]:
    """One frame as the peer sees it, parsed here — not by the module under test."""
    b1, b2 = _recv_exact(sock, 2)
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    key = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return {"fin": fin, "opcode": opcode, "masked": masked, "payload": payload}


def _server_frame(opcode: int, payload: bytes = b"", *, fin: bool = True, mask: bool = False) -> bytes:
    """The bytes a server would put on the wire for one frame."""
    header = bytearray()
    header.append((0x80 if fin else 0x00) | opcode)
    mask_bit = 0x80 if mask else 0x00
    length = len(payload)
    if length < 126:
        header.append(mask_bit | length)
    elif length < 0x10000:
        header.append(mask_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", length)
    if not mask:
        return bytes(header) + payload
    key = b"\x0a\x0b\x0c\x0d"
    return bytes(header) + key + bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))


def _handshaken(*, key: str = RFC_KEY, **response: Any) -> tuple["_ws.WebSocket", socket.socket, str]:
    """A client that has already completed a handshake against a fake server.

    The answer is staged before the client is asked to handshake because both
    ends live in one thread: the bytes sit in the socket's buffer and the client
    reads them the moment it has written its request. The request it wrote is
    read back afterwards, so the test still sees both directions.
    """
    client, server = _pair()
    _respond(server, key, **response)
    ws = _ws.WebSocket(client, host="gateway.test", timeout=READ_TIMEOUT)
    ws.handshake("/gateway", host="gateway.test", key=key)
    return ws, server, _read_head(server)


# -- the handshake ---------------------------------------------------------


def test_accept_key_matches_the_rfc_example() -> None:
    """RFC 6455 §1.3's published key/accept pair — the formula, not ours."""
    assert _ws.accept_key(RFC_KEY) == RFC_ACCEPT


def test_new_client_key_is_16_random_bytes_base64() -> None:
    """The nonce is 16 bytes and different every time — never a constant."""
    first = _ws.new_client_key()
    assert base64.b64decode(first) != base64.b64decode(_ws.new_client_key())
    assert len(base64.b64decode(first)) == 16


def test_handshake_accepts_a_correct_accept() -> None:
    """A compliant answer, in a different case and order, is accepted."""
    ws, _server, request = _handshaken()

    assert request.startswith("GET /gateway HTTP/1.1")
    assert _header(request, "host") == "gateway.test"
    assert _header(request, "sec-websocket-key") == RFC_KEY
    assert _header(request, "sec-websocket-version") == "13"
    assert _header(request, "upgrade") == "websocket"
    assert _header(request, "connection").lower() == "upgrade"
    assert ws.host == "gateway.test"
    assert ws.closed is False


def test_handshake_refuses_a_wrong_accept() -> None:
    """The one check a proxy cannot fake: the accept is this client's key hashed."""
    client, server = _pair()
    _respond(server, RFC_KEY, accept="AAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    ws = _ws.WebSocket(client, host="gateway.test", timeout=READ_TIMEOUT)

    with pytest.raises(_ws.WebSocketHandshakeError):
        ws.handshake("/gateway", host="gateway.test", key=RFC_KEY)


def test_handshake_refuses_an_accept_for_a_different_key() -> None:
    """An accept that is well-formed but belongs to another request is refused."""
    client, server = _pair()
    _respond(server, RFC_KEY, accept=_accept_for("some-other-nonce"))
    ws = _ws.WebSocket(client, host="gateway.test", timeout=READ_TIMEOUT)

    with pytest.raises(_ws.WebSocketHandshakeError):
        ws.handshake("/gateway", host="gateway.test", key=RFC_KEY)


def test_handshake_refuses_a_status_that_is_not_101() -> None:
    """A refusal is reported as the peer's own status, not as a success."""
    client, server = _pair()
    _respond(server, RFC_KEY, status="401 Unauthorized")
    ws = _ws.WebSocket(client, host="gateway.test", timeout=READ_TIMEOUT)

    with pytest.raises(_ws.WebSocketHandshakeError) as caught:
        ws.handshake("/gateway", host="gateway.test", key=RFC_KEY)
    assert "401" in str(caught.value)


def test_handshake_refuses_a_101_that_never_agreed_to_upgrade() -> None:
    """``101`` without ``Upgrade: websocket`` is not an upgrade."""
    client, server = _pair()
    _respond(server, RFC_KEY, upgrade="h2c")
    ws = _ws.WebSocket(client, host="gateway.test", timeout=READ_TIMEOUT)

    with pytest.raises(_ws.WebSocketHandshakeError):
        ws.handshake("/gateway", host="gateway.test", key=RFC_KEY)


def test_connect_drives_the_handshake_over_an_injected_socket() -> None:
    """``connect(..., sock=...)`` is the Discord seam: no TLS, no socket opened.

    The fake server runs on a thread and answers only *after* reading the
    request, so the client has to send before it blocks — the ordering a
    pre-staged answer cannot prove.
    """
    client, server = _pair()
    server_side = _in_thread(lambda: _respond(server, _header(_read_head(server), "sec-websocket-key") or ""))

    ws = _ws.connect("wss://gateway.test/gateway", sock=client, timeout=READ_TIMEOUT)
    server_side()

    assert isinstance(ws, _ws.WebSocket)
    assert ws.host == "gateway.test"


def test_connect_refuses_a_cleartext_scheme_before_opening_anything() -> None:
    """``ws://`` is an error, not a silent downgrade — and it costs no socket."""
    with pytest.raises(_ws.WebSocketError) as caught:
        _ws.connect("ws://gateway.test/gateway")
    assert "wss" in str(caught.value)


def test_url_parse_reads_host_port_and_resource() -> None:
    assert _ws.parse_url("wss://gateway.test") == ("gateway.test", 443, "/")
    assert _ws.parse_url("wss://gateway.test/gateway") == ("gateway.test", 443, "/gateway")
    assert _ws.parse_url("wss://gateway.test:8443/x?v=9") == ("gateway.test", 8443, "/x?v=9")
    assert _ws.parse_url("wss://[::1]:8443/x") == ("::1", 8443, "/x")


def test_url_parse_refuses_nonsense() -> None:
    for url in ("gateway.test/gateway", "https://gateway.test/", "wss:///nohost"):
        with pytest.raises(_ws.WebSocketError):
            _ws.parse_url(url)


# -- sending ---------------------------------------------------------------


def test_masked_text_frame_round_trips() -> None:
    """A text send reaches the peer as one masked, final, TEXT frame."""
    ws, server, _request = _handshaken()
    ws.send_text("hello gateway")

    frame = _read_frame(server)
    assert frame["opcode"] == _ws.OP_TEXT
    assert frame["fin"] is True
    assert frame["masked"] is True, "a client frame MUST be masked (RFC 6455 §5.1)"
    assert frame["payload"] == b"hello gateway"


def test_a_long_text_frame_uses_the_extended_length() -> None:
    """Past 125 bytes the length moves into the 16-bit field, and still round-trips."""
    ws, server, _request = _handshaken()
    body = "x" * 400
    ws.send_text(body)

    frame = _read_frame(server)
    assert frame["masked"] is True
    assert frame["payload"] == body.encode("utf-8")


def test_two_messages_do_not_share_a_mask() -> None:
    """Each frame carries its own key — a reused mask is a weakness, not a shortcut."""
    ws, server, _request = _handshaken()
    ws.send_text("first")
    ws.send_text("first")

    keys = []
    for _ in range(2):
        # Re-read both frames' mask keys: identical payloads with identical masks
        # would mean the mask is a constant.
        b1, b2 = _recv_exact(server, 2)
        assert b1 & 0x0F == _ws.OP_TEXT and b2 & 0x80
        keys.append(_recv_exact(server, 4))
        _recv_exact(server, b2 & 0x7F)
    assert keys[0] != keys[1]


# -- receiving -------------------------------------------------------------


def test_ping_is_answered_with_a_pong_carrying_the_same_payload() -> None:
    """A gateway's heartbeat is answered in kind, and the read carries on."""
    ws, server, _request = _handshaken()
    server.sendall(_server_frame(_ws.OP_PING, b"heartbeat") + _server_frame(_ws.OP_TEXT, b"after"))

    assert ws.recv_message() == "after"

    pong = _read_frame(server)
    assert pong["opcode"] == _ws.OP_PONG
    assert pong["payload"] == b"heartbeat"
    assert pong["masked"] is True


def test_a_pong_is_ignored() -> None:
    """An unsolicited pong is not a message and does not derail the read."""
    ws, server, _request = _handshaken()
    server.sendall(_server_frame(_ws.OP_PONG, b"noise") + _server_frame(_ws.OP_TEXT, b"payload"))

    assert ws.recv_message() == "payload"


def test_fragmented_message_reassembles() -> None:
    """Three frames, one message — the continuation frames are part of it."""
    ws, server, _request = _handshaken()
    server.sendall(
        _server_frame(_ws.OP_TEXT, b"Hel", fin=False)
        + _server_frame(_ws.OP_CONTINUATION, b"lo, ", fin=False)
        + _server_frame(_ws.OP_CONTINUATION, b"world", fin=True)
    )

    assert ws.recv_message() == "Hello, world"


def test_control_frame_between_fragments_is_answered_and_does_not_end_the_message() -> None:
    """A ping mid-fragment is handled, not mistaken for the message's end."""
    ws, server, _request = _handshaken()
    server.sendall(
        _server_frame(_ws.OP_TEXT, b"frag", fin=False)
        + _server_frame(_ws.OP_PING, b"mid")
        + _server_frame(_ws.OP_CONTINUATION, b"ment", fin=True)
    )

    assert ws.recv_message() == "fragment"

    pong = _read_frame(server)
    assert pong["opcode"] == _ws.OP_PONG
    assert pong["payload"] == b"mid"


def test_binary_frames_come_back_as_bytes() -> None:
    """A binary message is not decoded — it was never text."""
    ws, server, _request = _handshaken()
    server.sendall(_server_frame(_ws.OP_BINARY, b"\x00\x01\xfe\xff"))

    assert ws.recv_message() == b"\x00\x01\xfe\xff"


def test_text_that_is_not_utf8_is_a_protocol_error() -> None:
    """RFC 6455 §8.1: a text message must be valid UTF-8."""
    ws, server, _request = _handshaken()
    server.sendall(_server_frame(_ws.OP_TEXT, b"\xff\xfe"))

    with pytest.raises(_ws.WebSocketProtocolError):
        ws.recv_message()


# -- closing ---------------------------------------------------------------


def test_close_is_surfaced_with_the_peers_code_and_reason() -> None:
    """A close frame is the connection's answer, and it carries the peer's words."""
    ws, server, _request = _handshaken()
    server.sendall(_server_frame(_ws.OP_CLOSE, struct.pack("!H", 1000) + b"bye"))

    with pytest.raises(_ws.WebSocketClosed) as caught:
        ws.recv_message()

    assert caught.value.code == 1000
    assert caught.value.reason == "bye"
    echo = _read_frame(server)
    assert echo["opcode"] == _ws.OP_CLOSE, "RFC 6455 §5.5.1 requires the close to be echoed"


def test_close_without_a_code_is_reported_as_1005() -> None:
    """An empty close payload says "no code", which is 1005 — not "vanished" (1006)."""
    ws, server, _request = _handshaken()
    server.sendall(_server_frame(_ws.OP_CLOSE, b""))

    with pytest.raises(_ws.WebSocketClosed) as caught:
        ws.recv_message()
    assert caught.value.code == 1005


def test_a_vanished_peer_is_a_close_not_a_hang() -> None:
    """A peer that just goes away surfaces 1006 rather than blocking the caller."""
    ws, server, _request = _handshaken()
    server.close()

    with pytest.raises(_ws.WebSocketClosed) as caught:
        ws.recv_message()
    assert caught.value.code == 1006


def test_close_sends_one_close_frame_and_is_idempotent() -> None:
    """Calling close twice does not put two close frames on the wire."""
    ws, server, _request = _handshaken()
    ws.close(1001, "going away")
    ws.close(1001, "going away")

    frame = _read_frame(server)
    assert frame["opcode"] == _ws.OP_CLOSE
    assert frame["payload"] == struct.pack("!H", 1001) + b"going away"
    assert frame["masked"] is True
    assert ws.closed is True


def test_close_refuses_a_reason_that_would_overflow_a_control_frame() -> None:
    """123 bytes of reason is the ceiling; more is a caller error, not a truncation."""
    ws, _server, _request = _handshaken()
    with pytest.raises(ValueError):
        ws.close(1000, "y" * 200)


# -- frames a compliant peer would never send ------------------------------


def test_a_masked_server_frame_is_refused() -> None:
    """RFC 6455 §5.1: a server must not mask — a frame that is might be a replay."""
    ws, server, _request = _handshaken()
    server.sendall(_server_frame(_ws.OP_TEXT, b"masked", mask=True))

    with pytest.raises(_ws.WebSocketProtocolError):
        ws.recv_message()


def test_a_reserved_bit_is_refused() -> None:
    """No extension was negotiated, so RSV1 cannot mean anything this client reads."""
    ws, server, _request = _handshaken()
    server.sendall(bytes([0x80 | 0x40 | _ws.OP_TEXT, 1]) + b"x")

    with pytest.raises(_ws.WebSocketProtocolError):
        ws.recv_message()


def test_a_fragmented_control_frame_is_refused() -> None:
    """A control frame may not be fragmented, and one that is is a protocol error."""
    ws, server, _request = _handshaken()
    server.sendall(_server_frame(_ws.OP_PING, b"part", fin=False))

    with pytest.raises(_ws.WebSocketProtocolError):
        ws.recv_message()


def test_a_continuation_with_nothing_to_continue_is_refused() -> None:
    """The read path may not attach a fragment to a message that never started."""
    ws, server, _request = _handshaken()
    server.sendall(_server_frame(_ws.OP_CONTINUATION, b"orphan", fin=True))

    with pytest.raises(_ws.WebSocketProtocolError):
        ws.recv_message()


def test_an_oversized_frame_is_refused_before_it_is_read() -> None:
    """A declared length is a request to allocate; the cap answers for this side."""
    ws, server, _request = _handshaken()
    server.sendall(bytes([0x80 | _ws.OP_BINARY, 127]) + struct.pack("!Q", 2**40))

    with pytest.raises(_ws.WebSocketProtocolError):
        ws.recv_message()


def test_a_frame_mixed_with_the_handshake_is_not_lost() -> None:
    """Bytes after the handshake in the same segment are the first frame, not debris.

    A server may put its first frame in the same TCP segment as its answer; the
    head reader must stop at the blank line and keep the rest.
    """
    client, server = _pair()
    _respond(server, RFC_KEY)
    server.sendall(_server_frame(_ws.OP_TEXT, b"pushed with the handshake"))
    ws = _ws.WebSocket(client, host="gateway.test", timeout=READ_TIMEOUT)
    ws.handshake("/gateway", host="gateway.test", key=RFC_KEY)

    assert ws.recv_message() == "pushed with the handshake"
