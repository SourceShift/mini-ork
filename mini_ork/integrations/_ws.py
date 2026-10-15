"""WebSocket, the client half: RFC 6455 over the standard library alone.

The chat transports under this package all speak HTTP — Telegram long-polls,
Slack posts, a webhook catches what a relay pushes. The two that do not
(Slack's socket mode and Discord's gateway) hand the conversation back over a
*persistent* connection instead, and that is what this module is: the client
end of RFC 6455, small enough to read, with no dependency the runtime does not
already have.

**Why it is hand-written.** ``pyproject.toml``'s runtime dependencies are
``PyYAML`` and ``jsonschema`` and this module does not move them. ``websocket-
client`` would be a third, and the whole reason the transports here are stdlib
is that "install mini-ork and talk to your chat surface" has to be one step.
The protocol the clients actually need is four things — an opening handshake, a
masked frame send, a reassembling read, and the three control frames — so the
module implements those and refuses the rest (extensions, subprotocols, the
cleartext ``ws://`` scheme) rather than growing a second, worse copy of a
library that exists.

**Fail closed, and name the condition, never the value.** A URL is refused
unless its scheme is ``wss://``: a cleartext WebSocket is the conversation on
the wire in the open, and a client that quietly downgraded would be the sort of
"works on my laptop" default this repo keeps out of the secure tier. No error
message here renders the URL, the client key or the accept value — gateway URLs
carry tokens in their query string, exactly as the Telegram transport's request
URLs carry the bot token, and a traceback that echoes one is a credential in a
log file. What a message *does* carry is which check failed.

**TLS is confined to :func:`connect`.** The socket is not opened inside the
client protocol: :class:`WebSocket` is handed a socket and drives the handshake
and the framing over whatever that socket is. That is the seam two callers need
— a Discord transport "driving a fake ``_ws`` connection", and this module's own
tests, which never touch the network (``socket.socketpair()`` is a real,
connected socket and the frame engine cannot tell the difference). ``connect``
is then the thin convenience that opens a TCP socket, wraps it in TLS, and hands
it to the same engine.

**What is deliberately missing.** Extensions and ``permessage-deflate`` (RSV
bits are refused rather than ignored), subprotocol negotiation, an ``ws://``
tier, and proxy support. Each is a real feature of the protocol and none is a
feature of the gateway connection the transports need; adding one later is an
edit here, not a dependency.
"""

import base64
import hashlib
import os
import socket
import ssl
import struct

#: The name every error message in this module is prefixed with. A constant
#: because it is also what a caller greps for, and one string, one meaning.
NAME = "websocket"

#: The RFC 6455 §1.3 magic string. Concatenated with the client's key and
#: hashed, it is what proves the peer read the request rather than being a
#: proxy that answered ``101`` to anything.
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: Frame opcodes, RFC 6455 §5.2. The high bit of the first byte is FIN and the
#: three below it are RSV, so an opcode is the low nibble and nothing else.
OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: How long any one socket operation may block when the caller does not say.
#: Not a nicety: a gateway that goes silent mid-read must fail the caller rather
#: than pin the thread that is running it, and a half-closed ``socketpair`` in a
#: test must raise instead of hanging the suite.
DEFAULT_TIMEOUT = 30.0

#: How large the opening handshake's head may grow before it is refused. A peer
#: that never sends the terminating blank line must not be able to make this
#: client allocate without bound.
MAX_HEADER_BYTES = 64 * 1024

#: The largest message, and the largest frame, this client will assemble. A peer
#: declares its payload length in the frame header, so an unchecked read of a
#: declared ``2**63`` is an allocation the remote side asks for and this side
#: pays. The cap is generous for a chat payload and finite for a hostile peer.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024

#: A close frame's payload is a 2-byte status code plus a UTF-8 reason, and no
#: control frame may exceed 125 bytes (RFC 6455 §5.5) — so a reason is at most
#: 123 bytes, and a longer one is a caller error rather than something to
#: silently truncate.
MAX_CLOSE_REASON_BYTES = 123


class WebSocketError(RuntimeError):
    """A WebSocket client could not do its job. Names the condition, not a value."""


class WebSocketHandshakeError(WebSocketError):
    """The opening handshake was refused, or its answer did not verify.

    The distinction the acceptance check exists for: a peer that answers ``101``
    without proving it read this client's key is not a WebSocket server, and
    this client has no way to tell it apart from one that is lying.
    """


class WebSocketProtocolError(WebSocketError):
    """A frame on the wire broke RFC 6455 — a hostile or a broken peer."""


class WebSocketClosed(WebSocketError):
    """The connection is closed: a close frame arrived, or the peer vanished.

    ``code`` and ``reason`` are the peer's. ``1006`` is RFC 6455's own answer for
    "closed with no close frame" — the value a peer that hung up leaves, and one
    that is never sent on the wire, which is why it is the default here rather
    than an invented sentinel.
    """

    def __init__(self, code: int = 1006, reason: str = "") -> None:
        self.code = int(code)
        self.reason = reason or ""
        if self.reason:
            detail = f" (code {self.code}, {self.reason!r})"
        else:
            detail = f" (code {self.code})"
        super().__init__(f"{NAME}: the connection is closed{detail}")


def accept_key(client_key: str) -> str:
    """The ``Sec-WebSocket-Accept`` value ``client_key`` must be answered with.

    RFC 6455 §4.2.2: ``base64(sha1(key + GUID))``. It is a hash, not a
    signature — it proves the peer read *this request*, which is all the
    handshake ever claimed.
    """
    digest = hashlib.sha1((client_key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def new_client_key() -> str:
    """A fresh handshake nonce: 16 random bytes, base64.

    ``os.urandom`` rather than :mod:`random`, because the key is the only thing
    standing between this client and a peer that answers ``101`` to any request
    it is handed.
    """
    return base64.b64encode(os.urandom(16)).decode("ascii")


def parse_url(url: str) -> tuple[str, int, str]:
    """``(host, port, resource)`` for a ``wss://`` URL, or a refusal.

    Hand-split rather than handed to :mod:`urllib.parse` because the shape this
    module needs is small — a scheme it insists on, an authority, a resource —
    and the module's whole point is to import the protocol core and nothing
    else. The refusal is the load-bearing half: ``ws://`` is not supported and
    is not silently upgraded to "just don't use TLS", it is an error, because
    the failure mode of accepting it is a conversation in the clear.
    """
    scheme, sep, rest = url.partition("://")
    if not sep or not scheme:
        raise WebSocketError(
            f"{NAME}: not a WebSocket URL — write it as wss://host/path"
        )
    if scheme.lower() != "wss":
        raise WebSocketError(
            f"{NAME}: {scheme.lower()!r} is not a scheme this client opens — only "
            "wss:// is supported, and a cleartext WebSocket would put the "
            "conversation on the wire in the open"
        )
    authority, slash, tail = rest.partition("/")
    if not authority:
        raise WebSocketError(f"{NAME}: the URL names no host — write it as wss://host/path")
    host, port = _split_authority(authority)
    if not host:
        raise WebSocketError(f"{NAME}: the URL names no host — write it as wss://host/path")
    return host, port, f"/{tail}" if slash else "/"


def _split_authority(authority: str, default_port: int = 443) -> tuple[str, int]:
    """``(host, port)`` for ``authority`` — ``host``, ``host:port`` or ``[v6]:port``.

    Userinfo, when a URL carries it, is dropped rather than refused: this client
    authenticates with the transport's own token in a message or a query string,
    never with a URL credential, so the prefix is not a feature being ignored —
    it is a shape the parse must survive. Nothing here is ever rendered.
    """
    host_port = authority.rsplit("@", 1)[-1]
    if host_port.startswith("["):
        host, _, remainder = host_port[1:].partition("]")
        port_text = remainder[1:] if remainder.startswith(":") else ""
        if not port_text:
            return host, default_port
    else:
        host, sep, port_text = host_port.rpartition(":")
        if not sep:
            return host_port, default_port
    try:
        return host, int(port_text)
    except ValueError as exc:
        raise WebSocketError(
            f"{NAME}: the port in the URL is not a number — write it as wss://host:port/path"
        ) from exc


def connect(
    url: str,
    *,
    sock: "socket.socket | None" = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    context: "ssl.SSLContext | None" = None,
    headers: "dict[str, str] | None" = None,
    key: str | None = None,
) -> "WebSocket":
    """Open ``url`` and complete the opening handshake. The one TLS seam.

    ``sock`` is the escape hatch that makes everything else testable: when it is
    given, no TCP connection is opened and no TLS is negotiated — the handshake
    and the framing run over *that* socket, whatever it is. A ``socketpair``, a
    socket a test has already driven, or a connection a Discord transport built
    itself all work, and the frame engine cannot tell any of them from a gateway.

    When it is not given, the socket is opened and wrapped per the URL's
    ``wss``, with a default context that verifies the peer's certificate and its
    hostname. That verification is not configurable here by a flag, only by
    passing a ``context``: the failure it prevents is a chat token sent to
    whoever answered on the wire.
    """
    host, port, resource = parse_url(url)
    if sock is None:
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            tls = ssl.create_default_context() if context is None else context
            sock = tls.wrap_socket(sock, server_hostname=host)
        except Exception:
            # The TCP connection exists and owns an fd; a failed handshake must
            # not leak it on the way to raising.
            sock.close()
            raise
        if timeout is not None:
            sock.settimeout(timeout)
    client = WebSocket(sock, host=host, timeout=timeout)
    client.handshake(resource, host=host, port=port, headers=headers, key=key)
    return client


class WebSocket:
    """A WebSocket connection: the opening handshake, then frames.

    Constructed around a socket it does not own — closing this object does not
    close the socket, and closing the socket closes this object (the next read
    surfaces :class:`WebSocketClosed`). That split is what lets a caller park one
    of these on a socket it built, and what lets the tests drive it over a
    ``socketpair``.

    Every read is bounded: ``timeout`` is applied to the socket at construction
    so a peer that stops talking raises rather than parking the thread that is
    running the connection.
    """

    def __init__(
        self,
        sock: "socket.socket",
        *,
        host: str | None = None,
        timeout: float | None = DEFAULT_TIMEOUT,
    ) -> None:
        self._sock = sock
        self._host = host
        self._buffer = bytearray()
        self._key: str | None = None
        self._close_sent = False
        if timeout is not None:
            sock.settimeout(timeout)

    @property
    def host(self) -> str | None:
        """The host this connection was opened against, for a log line."""
        return self._host

    @property
    def closed(self) -> bool:
        """Whether this side has sent (or echoed) a close frame."""
        return self._close_sent

    def __repr__(self) -> str:
        # The host and nothing else: a gateway URL can carry a token, and a
        # repr is what reaches a log line and a traceback.
        return f"<WebSocket host={self._host!r} closed={self._close_sent}>"

    # -- the opening handshake ---------------------------------------------
    def handshake(
        self,
        resource: str = "/",
        *,
        host: str | None = None,
        port: int | None = None,
        headers: "dict[str, str] | None" = None,
        key: str | None = None,
    ) -> dict[str, str]:
        """Send the upgrade request and verify the answer, or raise.

        Returns the response's headers (lower-cased names) so a caller can read
        anything else the peer volunteered. The four checks are the handshake:
        a ``101`` status, ``Upgrade: websocket``, an ``Upgrade`` token in
        ``Connection``, and an ``Sec-WebSocket-Accept`` that equals
        :func:`accept_key` of this request's key. The last one is the only one a
        proxy cannot fake, which is why it is checked rather than assumed.

        ``key`` is pinnable, and that is not only for tests: a caller that wants
        the handshake to be reproducible — the RFC's own example, a golden
        fixture — passes the nonce instead of accepting the random one.
        """
        host = host or self._host
        if not host:
            raise WebSocketHandshakeError(
                f"{NAME}: the handshake needs a Host header — construct the client "
                "with host= or pass it here"
            )
        resource = resource or "/"
        key = key or new_client_key()
        lines = [
            f"GET {resource} HTTP/1.1",
            f"Host: {host if port in (None, 443) else f'{host}:{port}'}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for name, value in (headers or {}).items():
            lines.append(f"{name}: {value}")
        self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))

        status, response = self._read_head()
        code = _status_code(status)
        if code != 101:
            # The status line is the peer's own words — the one thing that
            # turns "the handshake failed" into an actionable sentence — and it
            # never carries this client's key or the request it refused.
            raise WebSocketHandshakeError(
                f"{NAME}: the handshake was refused — the peer answered {status.strip()!r}, "
                "and only 101 means it accepted"
            )
        if not _has_token(response.get("upgrade", ""), "websocket"):
            raise WebSocketHandshakeError(
                f"{NAME}: the handshake answered 101 without 'Upgrade: websocket' — "
                "refusing to speak to a peer that did not agree to the protocol"
            )
        if not _has_token(response.get("connection", ""), "upgrade"):
            raise WebSocketHandshakeError(
                f"{NAME}: the handshake answered 101 with no 'Upgrade' token in "
                "Connection — refusing to speak to a peer that did not agree to the protocol"
            )
        expected = accept_key(key)
        if response.get("sec-websocket-accept") != expected:
            # Deliberately value-free: both the key and the answer are inputs to
            # a hash of a nonce, but the habit is the point — an error message
            # in this module names the condition, never the bytes.
            raise WebSocketHandshakeError(
                f"{NAME}: the handshake's Sec-WebSocket-Accept does not match this "
                "client's key — the peer did not read this request"
            )
        self._key = key
        return response

    def _read_head(self) -> tuple[str, dict[str, str]]:
        """The response's status line and headers, and *only* those.

        Anything the peer sent after the head stays in ``self._buffer``. A server
        is allowed to put its first frame in the same TCP segment as its
        handshake answer, and a reader that consumed the rest of that segment as
        "part of the head" would drop it.
        """
        while True:
            end = bytes(self._buffer).find(b"\r\n\r\n")
            if end >= 0:
                break
            if len(self._buffer) > MAX_HEADER_BYTES:
                raise WebSocketHandshakeError(
                    f"{NAME}: the handshake's answer grew past {MAX_HEADER_BYTES} bytes "
                    "without a terminating blank line — refusing to read further"
                )
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WebSocketHandshakeError(
                    f"{NAME}: the peer closed before the handshake's answer was complete"
                )
            self._buffer += chunk
        head = bytes(self._buffer[:end])
        del self._buffer[: end + 4]
        return _parse_head(head)

    # -- sending ------------------------------------------------------------
    def send_text(self, text: str) -> None:
        """One text message. Encoded UTF-8, masked, as RFC 6455 requires."""
        if not isinstance(text, str):
            raise TypeError(f"{NAME}: send_text takes str, not {type(text).__name__}")
        self.send_frame(OP_TEXT, text.encode("utf-8"))

    def send_ping(self, payload: bytes = b"") -> None:
        """A ping. The payload is opaque — the peer must echo it in its pong."""
        self.send_frame(OP_PING, payload)

    def send_pong(self, payload: bytes = b"") -> None:
        """A pong, unsolicited or answering a ping :meth:`recv_message` already saw."""
        self.send_frame(OP_PONG, payload)

    def send_frame(self, opcode: int, payload: bytes = b"", *, fin: bool = True) -> None:
        """One frame, always masked, always from one call.

        There is no ``mask=False`` here on purpose: a client that can send an
        unmasked frame will, eventually, and every RFC 6455 server — Slack's and
        Discord's included — drops the connection over it, silently. The flag is
        not offered, so it cannot be got wrong.
        """
        if isinstance(payload, str):  # a courtesy for a caller holding text
            payload = payload.encode("utf-8")
        self._sock.sendall(_frame(opcode, bytes(payload), fin=fin, mask=True))

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Send a close frame. Idempotent, and does not wait for the echo.

        The peer's answering close is what :meth:`recv_message` surfaces; a
        caller that wants to see it calls ``recv_message`` after this and
        catches :class:`WebSocketClosed`.
        """
        if self._close_sent:
            return
        payload = b""
        if code is not None:
            encoded = reason.encode("utf-8") if reason else b""
            if len(encoded) > MAX_CLOSE_REASON_BYTES:
                raise ValueError(
                    f"{NAME}: a close reason is at most {MAX_CLOSE_REASON_BYTES} bytes, "
                    f"not {len(encoded)} — a control frame may not exceed 125 bytes"
                )
            payload = struct.pack("!H", int(code)) + encoded
        self.send_frame(OP_CLOSE, payload)
        self._close_sent = True

    # -- receiving ----------------------------------------------------------
    def recv_message(self) -> "str | bytes":
        """The next data message, reassembled: ``str`` for text, ``bytes`` for binary.

        The loop is the point. A data message may arrive as a first frame and
        any number of continuations, and control frames may arrive *between*
        them — so a read that returned as soon as it had bytes would hand the
        caller half a message the first time a gateway pinged mid-payload. A
        ping is answered here (this is the only place the connection is read,
        so it is the only place it can be), a pong is ignored, a close is echoed
        and then surfaced as :class:`WebSocketClosed`, and only a completed data
        message is returned.
        """
        fragments = bytearray()
        message_opcode: int | None = None
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == OP_PING:
                self._echo(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code, reason = _parse_close(payload)
                self._echo_close(payload)
                raise WebSocketClosed(code, reason)
            if opcode == OP_CONTINUATION:
                if message_opcode is None:
                    raise WebSocketProtocolError(
                        f"{NAME}: a continuation frame arrived with no message to continue"
                    )
            elif opcode in (OP_TEXT, OP_BINARY):
                if message_opcode is not None:
                    raise WebSocketProtocolError(
                        f"{NAME}: a new data frame arrived in the middle of a "
                        "fragmented message — RFC 6455 forbids interleaving messages"
                    )
                message_opcode = opcode
            else:
                raise WebSocketProtocolError(
                    f"{NAME}: opcode {opcode:#x} is not one RFC 6455 defines"
                )
            fragments += payload
            if len(fragments) > MAX_MESSAGE_BYTES:
                raise WebSocketProtocolError(
                    f"{NAME}: a message grew past {MAX_MESSAGE_BYTES} bytes — refusing "
                    "to keep assembling it"
                )
            if not fin:
                continue
            data = bytes(fragments)
            if message_opcode == OP_TEXT:
                try:
                    return data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise WebSocketProtocolError(
                        f"{NAME}: a text message was not valid UTF-8"
                    ) from exc
            return data

    def _read_frame(self) -> tuple[bool, int, bytes]:
        """One frame off the wire, validated, unmasked. ``(fin, opcode, payload)``.

        The checks are RFC 6455's mandatory ones and each has a reason to be
        here rather than in the caller: RSV bits are refused because no
        extension was negotiated (so a peer setting them is speaking something
        else); a *masked* server frame is refused because a server must not mask
        (§5.1) and a frame that is might be a relay replaying a client's bytes;
        control frames are refused when fragmented or oversized because §5.5
        forbids both; and a declared length over the cap is refused *before* the
        read that would allocate it.
        """
        b1, b2 = self._recv_exact(2)
        fin = bool(b1 & 0x80)
        reserved = b1 & 0x70
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if reserved:
            raise WebSocketProtocolError(
                f"{NAME}: a frame set reserved bits ({reserved:#x}) — no extension was "
                "negotiated, so this client cannot read what it means"
            )
        if masked:
            raise WebSocketProtocolError(
                f"{NAME}: the peer masked a frame it sent to this client — RFC 6455 "
                "forbids a server frame from being masked"
            )
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        if opcode >= OP_CLOSE:
            if not fin:
                raise WebSocketProtocolError(
                    f"{NAME}: a control frame arrived fragmented — RFC 6455 forbids it"
                )
            if length > 125:
                raise WebSocketProtocolError(
                    f"{NAME}: a control frame declared {length} bytes — RFC 6455 caps "
                    "one at 125"
                )
        if length > MAX_MESSAGE_BYTES:
            raise WebSocketProtocolError(
                f"{NAME}: a frame declared {length} bytes, past this client's "
                f"{MAX_MESSAGE_BYTES}-byte limit — refusing to allocate it"
            )
        payload = self._recv_exact(length) if length else b""
        return fin, opcode, payload

    def _recv_exact(self, count: int) -> bytes:
        """Exactly ``count`` bytes, or :class:`WebSocketClosed` if the peer goes away.

        A short read means the peer stopped mid-frame: there is no "try again
        later" for a framing stream whose cursor is a byte position, so the
        honest answer is that the connection is gone.
        """
        out = bytearray()
        while len(out) < count:
            chunk = self._recv_some(count - len(out))
            if not chunk:
                raise WebSocketClosed(1006, "")
            out += chunk
        return bytes(out)

    def _recv_some(self, count: int) -> bytes:
        """Bytes from the connection: whatever the buffer holds first, then the socket.

        The buffer exists because the handshake may be followed in the same read
        by the first frame (see :meth:`_read_head`); a framing reader that only
        ever called ``recv`` would lose it. ``b""`` means the peer closed.
        """
        if self._buffer:
            chunk = bytes(self._buffer[:count])
            del self._buffer[:count]
            return chunk
        return self._sock.recv(count)

    def _echo(self, opcode: int, payload: bytes) -> None:
        """Answer a control frame, tolerating a peer that has already hung up.

        A pong into a closed socket raises ``BrokenPipeError``, and there is
        nothing useful to do with it: the next read reports the close, which is
        the error the caller actually needs to see.
        """
        try:
            self.send_frame(opcode, payload)
        except OSError:
            pass

    def _echo_close(self, payload: bytes) -> None:
        """Answer a close frame once — RFC 6455 §5.5.1 requires the echo."""
        if self._close_sent:
            return
        self._close_sent = True
        try:
            self.send_frame(OP_CLOSE, payload)
        except OSError:
            pass


def _frame(opcode: int, payload: bytes, *, fin: bool = True, mask: bool = True) -> bytes:
    """``payload`` as one frame: FIN and opcode, then the length, then the bytes.

    Three length forms, chosen by RFC 6455 §5.2: 7 bits inline, then ``126`` with
    a 2-byte length, then ``127`` with an 8-byte one. A masked frame carries a
    4-byte key before the payload and XORs it across, restarting every four
    bytes — the "mask" is not a secret and not a cipher, it is there so a
    misrouted frame cannot be mistaken for a proxy's or a cache's own words.
    """
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
    key = os.urandom(4)
    masked = bytes(byte ^ key[index & 3] for index, byte in enumerate(payload))
    return bytes(header) + key + masked


def _parse_head(head: bytes) -> tuple[str, dict[str, str]]:
    """``head`` as ``(status_line, headers)``, names lower-cased.

    Case- and order-insensitive on purpose: RFC 6455 §4.1 requires a client to
    read header names case-insensitively and to accept them in any order, and a
    client that split on an exact ``Sec-WebSocket-Accept:`` would refuse a
    compliant server that wrote the name differently.
    """
    lines = head.split(b"\n")
    status = lines[0].decode("latin-1", "replace").strip()
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        if not sep:
            continue
        headers[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return status, headers


def _status_code(status: str) -> int:
    """The numeric status in an HTTP status line, or a handshake refusal."""
    parts = status.split()
    if len(parts) < 2 or not parts[0].upper().startswith("HTTP/"):
        raise WebSocketHandshakeError(
            f"{NAME}: the peer's answer was not an HTTP status line — not a WebSocket server"
        )
    try:
        return int(parts[1])
    except ValueError as exc:
        raise WebSocketHandshakeError(
            f"{NAME}: the peer's status line carried no numeric status"
        ) from exc


def _has_token(value: str, token: str) -> bool:
    """Whether ``token`` is one of ``value``'s comma-separated tokens.

    ``Connection: keep-alive, Upgrade`` is a compliant answer, and a client that
    compared the whole field would call it a refusal.
    """
    return token.lower() in [part.strip().lower() for part in value.split(",")]


def _parse_close(payload: bytes) -> tuple[int, str]:
    """``(code, reason)`` from a close frame's payload.

    An empty payload is RFC 6455's "no status code" — reported as ``1005``, the
    value reserved for saying exactly that, since ``1006`` here would claim the
    peer vanished without one when it did say so.
    """
    if not payload:
        return 1005, ""
    if len(payload) == 1:
        raise WebSocketProtocolError(
            f"{NAME}: a close frame carried a 1-byte payload — a status code is 2 bytes"
        )
    code = struct.unpack("!H", payload[:2])[0]
    reason = payload[2:]
    if not reason:
        return code, ""
    try:
        return code, reason.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebSocketProtocolError(
            f"{NAME}: a close frame's reason was not valid UTF-8"
        ) from exc


__all__ = [
    "DEFAULT_TIMEOUT",
    "GUID",
    "MAX_CLOSE_REASON_BYTES",
    "MAX_HEADER_BYTES",
    "MAX_MESSAGE_BYTES",
    "NAME",
    "OP_BINARY",
    "OP_CLOSE",
    "OP_CONTINUATION",
    "OP_PING",
    "OP_PONG",
    "OP_TEXT",
    "WebSocket",
    "WebSocketClosed",
    "WebSocketError",
    "WebSocketHandshakeError",
    "WebSocketProtocolError",
    "accept_key",
    "connect",
    "new_client_key",
    "parse_url",
]
