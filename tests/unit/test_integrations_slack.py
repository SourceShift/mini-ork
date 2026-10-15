"""Tests for ``mini_ork.integrations.slack`` — both tiers, offline.

No network, no TLS, and no run is ever spawned. The socket-mode tier is driven
over a :func:`socket.socketpair`: the real :class:`~mini_ork.integrations._ws`
client does its real handshake and its real framing against a fake Slack server
that speaks just enough RFC 6455 (the same trick ``test_integrations_ws.py``
uses). ``urllib.request.urlopen`` is monkeypatched so ``apps.connections.open``
mints a URL and ``chat.postMessage`` records instead of posting, and
``control.launch_run`` / ``control.steer_run`` are stubbed with the fixtures
``tests/unit/test_integrations_telegram.py`` uses (the house pattern). The
threads DB is a real migrated ``state.db`` under ``tmp_path``, because the state
machine reads ``task_runs`` and settles a live run against it.

What is pinned:

* a first message from a channel opens the thread ``("slack", channel, None)``
  names and *launches*; a message inside a Slack thread opens the thread's own
  key (``thread_ts``, not ``ts``);
* a follow-up on the same key *steers* (a run is live) or *continues* (it
  settled) and never launches a second time — a chat is one conversation;
* every envelope is acked with its ``envelope_id`` — including one this
  transport ignores — and a redelivered event id is a *duplicate*, so the
  launcher is not reached twice;
* a bot message and a ``message_changed`` subtype are ignored, never delivered;
* the Events API tier verifies Slack's ``v0`` signature over the raw body,
  refuses a wrong signature and a timestamp outside the freshness window, and
  answers ``url_verification`` with its challenge;
* each of the three vault keys *fails closed* — the error names the key, and no
  secret value reaches ``__repr__`` or a message;
* ``send`` posts to the channel the thread's record names and projects the turn
  onto the transcript exactly once, even on a retry.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import sys
import threading
import time
import urllib.error
from pathlib import Path
from typing import Any, Callable

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.integrations import _ws  # noqa: E402
from mini_ork.integrations import slack  # noqa: E402
from mini_ork.integrations.slack import SlackTransport  # noqa: E402
from mini_ork.integrations.telegram import TelegramTransport  # noqa: E402
from mini_ork.stores import migrate as mig  # noqa: E402
from mini_ork.threads import store  # noqa: E402

CHANNEL = "C-1"
TOKEN = "xoxb-test-token-not-a-real-one"
APP_TOKEN = "xapp-test-token-not-a-real-one"
SECRET = "correct horse battery staple"

#: The URL ``apps.connections.open`` hands back. A real ``wss`` URL: the client
#: insists on the scheme before it opens anything (``_ws.parse_url``).
WS_URL = "wss://wss-primary.slack.com/link/?ticket=test-ticket"

READ_TIMEOUT = 5.0

#: The GUID is restated here rather than imported: it is the RFC's constant, and
#: a test that took it from the module could not catch the module getting it wrong.
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# -- fixtures --------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "proj" / ".mini-ork"
    h.mkdir(parents=True)
    rc, _, err = mig.init_db(db=str(h / "state.db"), root=str(REPO))
    assert rc == 0, err
    return h


@pytest.fixture
def db(home: Path) -> str:
    return str(home / "state.db")


@pytest.fixture
def launches(monkeypatch) -> list[dict]:
    """A ``control.launch_run`` that records instead of spawning."""
    import mini_ork.web.control as control

    calls: list[dict] = []

    def fake(home, recipe, kickoff, run_id=None):
        calls.append({"home": home, "recipe": recipe, "kickoff": kickoff, "run_id": run_id})
        return {
            "ok": True,
            "run_id": run_id,
            "recipe": recipe,
            "pid": 4242,
            "kickoff_path": str(Path(home) / "runs-inbox" / f"{run_id}.md"),
            "log_path": str(Path(home) / "logs" / f"{run_id}.log"),
        }

    monkeypatch.setattr(control, "launch_run", fake)
    return calls


@pytest.fixture
def steers(monkeypatch) -> list[dict]:
    """A ``control.steer_run`` that records instead of injecting."""
    import mini_ork.web.control as control

    calls: list[dict] = []

    def fake(db, task_run_id, message, **kwargs):
        calls.append({"run_id": task_run_id, "message": message, **kwargs})
        return {"ok": True, "steering_id": 7}

    monkeypatch.setattr(control, "steer_run", fake)
    return calls


class _FakeResponse:
    """The slice of an ``HTTPResponse`` the transport actually uses."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeAPI:
    """A stand-in for ``urllib.request.urlopen`` against the Slack Web API.

    Every request is recorded with its URL, headers and JSON body, so a test can
    assert what was sent — the app token on ``apps.connections.open``, the bot
    token and payload on ``chat.postMessage`` — without a network or a real
    credential.
    """

    def __init__(self, *, ws_url: str = WS_URL) -> None:
        self.ws_url = ws_url
        self.calls: list[dict] = []

    def install(self, monkeypatch) -> "_FakeAPI":
        def fake_urlopen(request, timeout=None):  # noqa: ARG001 — timeout is the API's
            url = getattr(request, "full_url", str(request))
            headers = {str(k).lower(): v for k, v in dict(request.headers).items()}
            raw = request.data
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            self.calls.append({"url": url, "headers": headers, "payload": payload})
            if url.endswith("apps.connections.open"):
                body = {"ok": True, "url": self.ws_url}
            elif url.endswith("chat.postMessage"):
                body = {"ok": True, "ts": "1700000000.000100", "channel": payload.get("channel")}
            else:
                body = {"ok": False, "error": "unknown_method"}
            return _FakeResponse(json.dumps(body).encode("utf-8"))

        monkeypatch.setattr(slack.urllib.request, "urlopen", fake_urlopen)
        return self

    def posts(self) -> list[dict]:
        return [c for c in self.calls if c["url"].endswith("chat.postMessage")]

    def opens(self) -> list[dict]:
        return [c for c in self.calls if c["url"].endswith("apps.connections.open")]


@pytest.fixture
def api(monkeypatch) -> _FakeAPI:
    return _FakeAPI().install(monkeypatch)


class _EmptyVault:
    """A vault with nothing in it — a missing key, not a broken one."""

    def resolve(self, key: str) -> None:  # noqa: ARG002 — the point is that it has none
        return None

    def describe(self) -> str:
        return "a test vault with nothing in it"


# -- the fake Slack socket server ------------------------------------------
# Written byte by byte here rather than reached for out of ``_ws``: a test that
# framed with the client's own writer would agree with itself whatever either did.


def _pair() -> tuple[socket.socket, socket.socket]:
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


def _respond(sock: socket.socket, key: str) -> None:
    """The handshake answer: 101, upgrade agreed, accept hashed from ``key``."""
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept}",
    ]
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))


def _server_frame(opcode: int, payload: bytes = b"") -> bytes:
    """The bytes a server would put on the wire: unmasked, one frame."""
    header = bytearray()
    header.append(0x80 | opcode)
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 0x10000:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    return bytes(header) + payload


def _read_frame(sock: socket.socket) -> dict[str, Any]:
    """One frame as the peer sees it, parsed here — not by the module under test."""
    b1, b2 = _recv_exact(sock, 2)
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
    return {"opcode": opcode, "payload": payload}


def _in_thread(fn: Callable[[], Any]) -> Callable[[], Any]:
    """Run ``fn`` on another thread; the returned callable joins and re-raises.

    Both ends of a ``socketpair`` live in one process, so the fake server has to
    answer from a thread while the client blocks reading. What the thread raises
    is re-raised by the join — a swallowed assertion would be exactly the false
    green this file exists to avoid.
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
        assert not thread.is_alive(), "the fake Slack server never finished"
        if "error" in box:
            raise box["error"]
        return box.get("value")

    return join


def _serve(server: socket.socket, envelopes: list[dict], *, ack_count: int) -> list[dict]:
    """Handshake, push every envelope, then read back ``ack_count`` acks.

    Returns the decoded ack frames so a test can assert each ``envelope_id`` was
    acknowledged. After the acks the server drains the client's close frame, so
    the client's own ``close()`` never races a closed socket.
    """
    _respond(server, _header(_read_head(server), "sec-websocket-key") or "")
    for envelope in envelopes:
        server.sendall(_server_frame(_ws.OP_TEXT, json.dumps(envelope).encode("utf-8")))
    acks = [json.loads(_read_frame(server)["payload"].decode("utf-8")) for _ in range(ack_count)]
    try:
        while _read_frame(server)["opcode"] != _ws.OP_CLOSE:
            pass
    except Exception:  # noqa: BLE001 — a peer that hung up ends the drain, not the test
        pass
    server.close()
    return acks


def _socket_tick(
    transport: SlackTransport,
    envelopes: list[dict],
    *,
    ack_count: int | None = None,
    **poll_kwargs: Any,
) -> tuple[Any, list[dict]]:
    """One ``poll_once`` over a real socketpair against a fake Slack server."""
    client, server = _pair()
    expected = len(envelopes) if ack_count is None else ack_count
    join = _in_thread(lambda: _serve(server, envelopes, ack_count=expected))
    kwargs: dict[str, Any] = {"sock": client, "limit": len(envelopes)}
    kwargs.update(poll_kwargs)
    result = transport.poll_once(**kwargs)
    return result, join()


# -- envelope builders -----------------------------------------------------


def _envelope(
    event: dict,
    *,
    event_id: str = "Ev-1",
    envelope_id: str = "env-1",
) -> dict:
    """A Socket Mode ``events_api`` envelope wrapping one Events API body."""
    return {
        "type": "events_api",
        "envelope_id": envelope_id,
        "accepts_response_payload": False,
        "payload": _event_body(event, event_id=event_id),
    }


def _event_body(event: dict, *, event_id: str = "Ev-1") -> dict:
    """The Events API body — what a webhook POSTs and Socket Mode wraps."""
    return {
        "type": "event_callback",
        "team_id": "T-1",
        "api_app_id": "A-1",
        "event_id": event_id,
        "event_time": 1700000000,
        "event": event,
    }


def _message(
    text: str,
    *,
    channel: str = CHANNEL,
    ts: str = "1700000000.000100",
    thread_ts: str | None = None,
    user: str = "U-1",
    **extra: Any,
) -> dict:
    event: dict[str, Any] = {
        "type": "message",
        "channel": channel,
        "user": user,
        "text": text,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    event.update(extra)
    return event


def _transport(**kwargs: Any) -> SlackTransport:
    kwargs.setdefault("bot_token", TOKEN)
    kwargs.setdefault("app_token", APP_TOKEN)
    kwargs.setdefault("signing_secret", SECRET)
    return SlackTransport(**kwargs)


# -- the socket-mode tier --------------------------------------------------


def test_first_message_launches_the_thread(home: Path, db: str, launches, api) -> None:
    """A first message opens ``slack-<channel>``, launches, and is acked."""
    transport = _transport()
    result, acks = _socket_tick(transport, [_envelope(_message("deploy is red"))], home=home, db=db)

    assert result.action == "launched"
    assert result.delivery is not None
    assert result.delivery.thread_id == slack.thread_key("slack", CHANNEL, None)
    # No cursor on a push stream: the offset is None, not a fabricated number.
    assert result.next_offset is None
    assert [a["envelope_id"] for a in acks] == ["env-1"], "the envelope was never acked"
    assert len(launches) == 1
    assert launches[0]["run_id"] == result.delivery.thread_id

    # The app-level token is what opens the socket, not the bot token.
    openings = api.opens()
    assert openings, "apps.connections.open was never called"
    assert openings[0]["headers"]["authorization"] == f"Bearer {APP_TOKEN}"


def test_a_threaded_message_uses_the_thread_ts_not_the_ts(
    home: Path, db: str, launches, api
) -> None:
    """Slack's ``thread_ts`` is the sub-thread; ``ts`` is this message's own id."""
    transport = _transport()
    event = _message("in a thread", thread_ts="1700000000.000050", ts="1700000000.000100")

    result, _acks = _socket_tick(transport, [_envelope(event)], home=home, db=db)

    assert result.delivery is not None
    assert result.delivery.thread_id == slack.thread_key("slack", CHANNEL, "1700000000.000050")


def test_followup_never_launches_a_second_run(
    home: Path, db: str, launches, steers, api
) -> None:
    """A second message on the same key steers — a chat is one conversation."""
    transport = _transport()
    envelopes = [
        _envelope(_message("first"), event_id="Ev-a", envelope_id="env-a"),
        _envelope(_message("second", ts="1700000000.000200"), event_id="Ev-b", envelope_id="env-b"),
    ]

    result, _acks = _socket_tick(transport, envelopes, home=home, db=db)

    assert result.action in ("steered", "continued")
    assert result.action != "launched"
    assert len(launches) == 1, "a follow-up spawned a second run"
    assert [r.action for r in result.batch] == ["launched", result.action]


def test_redelivered_event_is_a_duplicate(home: Path, db: str, launches, api) -> None:
    """The same event id twice is a no-op — Slack redelivers until it is acked."""
    transport = _transport()
    first = _envelope(_message("only once"), event_id="Ev-9", envelope_id="env-9")
    # Byte-for-byte the same event, delivered again under a new envelope id.
    again = _envelope(_message("only once"), event_id="Ev-9", envelope_id="env-10")

    result, acks = _socket_tick(transport, [first, again], home=home, db=db)

    assert result.action == "duplicate"
    assert [r.action for r in result.batch] == ["launched", "duplicate"]
    assert len(launches) == 1, "a redelivery reached the launcher a second time"
    # A redelivery is still acked — Slack would otherwise redeliver it forever.
    assert [a["envelope_id"] for a in acks] == ["env-9", "env-10"]


def test_bot_messages_and_subtypes_are_ignored_but_acked(
    home: Path, db: str, launches, api
) -> None:
    """A bot's post and an edit are not deliveries — and still get their ack."""
    transport = _transport()
    envelopes = [
        _envelope(_message("beep", bot_id="B-1"), event_id="Ev-bot", envelope_id="env-bot"),
        _envelope(
            _message("edited", subtype="message_changed"),
            event_id="Ev-edit",
            envelope_id="env-edit",
        ),
        _envelope(_message("a real person"), event_id="Ev-real", envelope_id="env-real"),
    ]

    result, acks = _socket_tick(transport, envelopes, home=home, db=db)

    assert [r.action for r in result.batch] == ["ignored", "ignored", "launched"]
    assert [a["envelope_id"] for a in acks] == ["env-bot", "env-edit", "env-real"]
    assert len(launches) == 1


def test_a_hello_frame_is_ignored_and_needs_no_ack(
    home: Path, db: str, launches, api
) -> None:
    """A ``hello`` carries no ``envelope_id``, so there is nothing to ack."""
    transport = _transport()
    result, acks = _socket_tick(
        transport, [{"type": "hello", "num_connections": 1}], ack_count=0, home=home, db=db
    )

    assert result.action == "ignored"
    assert acks == []
    assert launches == []


# -- fail-closed configuration --------------------------------------------


def test_missing_bot_token_fails_closed(monkeypatch) -> None:
    """No bot token, no transport — and the error names the key a person must set."""
    monkeypatch.setattr(slack, "vault_environment", lambda *a, **k: _EmptyVault())

    with pytest.raises(slack.SlackConfigError) as caught:
        SlackTransport(bot_token=None)

    assert slack.BOT_TOKEN_VAULT_KEY in str(caught.value)
    assert "never falls back to an empty token" in str(caught.value)


def test_empty_bot_token_is_not_a_token(monkeypatch) -> None:
    """An explicit ``""`` is refused rather than used — the empty-string footgun."""
    monkeypatch.setattr(slack, "vault_environment", lambda *a, **k: _EmptyVault())

    with pytest.raises(slack.SlackConfigError):
        SlackTransport(bot_token="")


def test_missing_app_token_fails_closed(monkeypatch) -> None:
    """Socket mode with no resolvable app token refuses, naming the key."""
    monkeypatch.setattr(slack, "vault_environment", lambda *a, **k: _EmptyVault())
    transport = SlackTransport(bot_token=TOKEN)

    with pytest.raises(slack.SlackConfigError) as caught:
        transport.poll_once(home=None, db=None, sock=None, limit=1)

    assert slack.APP_TOKEN_VAULT_KEY in str(caught.value)


def test_missing_signing_secret_fails_closed(monkeypatch, home: Path, db: str) -> None:
    """A webhook with no resolvable secret refuses rather than accepting blind."""
    monkeypatch.setattr(slack, "vault_environment", lambda *a, **k: _EmptyVault())
    transport = SlackTransport(bot_token=TOKEN)
    body = json.dumps(_event_body(_message("anonymous"))).encode("utf-8")

    with pytest.raises(slack.SlackConfigError) as caught:
        transport.handle_webhook(body, "v0=deadbeef", timestamp="1", home=home, db=db)

    assert slack.SIGNING_SECRET_VAULT_KEY in str(caught.value)


def test_no_secret_appears_in_repr_or_a_message(monkeypatch) -> None:
    """The tokens are held, never rendered — repr and errors say the key, not the value."""
    monkeypatch.setattr(slack, "vault_environment", lambda *a, **k: _EmptyVault())
    transport = SlackTransport(bot_token=TOKEN, app_token=APP_TOKEN, signing_secret=SECRET)

    rendered = repr(transport)
    assert TOKEN not in rendered
    assert APP_TOKEN not in rendered
    assert SECRET not in rendered
    assert "***" in rendered

    with pytest.raises(slack.SlackConfigError) as caught:
        SlackTransport(bot_token=None)
    assert TOKEN not in str(caught.value)


# -- the webhook tier ------------------------------------------------------


def test_webhook_verifies_then_launches(home: Path, db: str, launches) -> None:
    """A body signed with the signing secret is accepted and delivered."""
    transport = _transport()
    body = json.dumps(_event_body(_message("from a webhook"))).encode("utf-8")
    stamp = str(int(time.time()))

    delivery = transport.handle_webhook(
        body, slack.sign_v0(SECRET, body, stamp), timestamp=stamp, home=home, db=db
    )

    assert delivery is not None
    assert delivery.action == "launched"
    assert delivery.thread_id == slack.thread_key("slack", CHANNEL, None)
    assert len(launches) == 1


def test_webhook_rejects_a_wrong_signature(home: Path, db: str, launches) -> None:
    """The signature covers the bytes and the secret — a wrong one is a refusal."""
    transport = _transport()
    body = json.dumps(_event_body(_message("honest"))).encode("utf-8")
    stamp = str(int(time.time()))

    with pytest.raises(slack.SlackSignatureError):
        transport.handle_webhook(
            body,
            slack.sign_v0("not-the-secret", body, stamp),
            timestamp=stamp,
            home=home,
            db=db,
        )
    assert launches == []


def test_webhook_rejects_a_stale_timestamp(home: Path, db: str, launches) -> None:
    """A valid signature over a replayed old request is still refused."""
    transport = _transport()
    body = json.dumps(_event_body(_message("replayed"))).encode("utf-8")
    stale = str(int(time.time()) - (slack.MAX_SKEW_SECONDS + 60))

    with pytest.raises(slack.SlackSignatureError):
        transport.handle_webhook(
            body, slack.sign_v0(SECRET, body, stale), timestamp=stale, home=home, db=db
        )
    assert launches == []


def test_webhook_rejects_a_missing_signature(home: Path, db: str, launches) -> None:
    """No signature is not "unsigned is fine" — it is a refusal."""
    body = json.dumps(_event_body(_message("anonymous"))).encode("utf-8")

    with pytest.raises(slack.SlackSignatureError):
        _transport().handle_webhook(
            body, None, timestamp=str(int(time.time())), home=home, db=db
        )
    assert launches == []


def test_webhook_ignores_a_bot_message(home: Path, db: str, launches) -> None:
    """A verified body that carries a bot's post delivers nothing."""
    transport = _transport()
    body = json.dumps(_event_body(_message("beep", bot_id="B-1"))).encode("utf-8")
    stamp = str(int(time.time()))

    delivery = transport.handle_webhook(
        body, slack.sign_v0(SECRET, body, stamp), timestamp=stamp, home=home, db=db
    )

    assert delivery is None
    assert launches == []


def test_url_verification_returns_its_challenge(home: Path, db: str) -> None:
    """The handshake is answered with its challenge — and is not a delivery."""
    transport = _transport()
    body = json.dumps(
        {"type": "url_verification", "challenge": "chal-3eZb", "token": "t"}
    ).encode("utf-8")
    stamp = str(int(time.time()))
    signature = slack.sign_v0(SECRET, body, stamp)

    assert transport.webhook_challenge(body, signature, timestamp=stamp) == "chal-3eZb"
    assert transport.handle_webhook(body, signature, timestamp=stamp, home=home, db=db) is None

    # And a delivery is not a challenge.
    event_body = json.dumps(_event_body(_message("hi"))).encode("utf-8")
    event_sig = slack.sign_v0(SECRET, event_body, stamp)
    assert transport.webhook_challenge(event_body, event_sig, timestamp=stamp) is None


def test_url_verification_still_needs_a_signature(home: Path, db: str) -> None:
    """A forged "handshake" gets the same refusal a forged delivery does."""
    transport = _transport()
    body = json.dumps({"type": "url_verification", "challenge": "chal"}).encode("utf-8")

    with pytest.raises(slack.SlackSignatureError):
        transport.webhook_challenge(body, "v0=forged", timestamp=str(int(time.time())))


def test_verify_v0_refuses_a_timestamp_that_is_not_a_number() -> None:
    """A junk timestamp is a refusal, not a crash."""
    assert slack.verify_v0(SECRET, b"{}", "v0=x", "not-a-number") is False
    assert slack.verify_v0(SECRET, b"{}", None, str(int(time.time()))) is False


# -- outbound --------------------------------------------------------------


def test_send_posts_and_projects_the_transcript_once(
    home: Path, db: str, launches, api
) -> None:
    """``send`` posts to the channel and records the turn — a retry does neither twice."""
    transport = _transport()
    launched, _acks = _socket_tick(transport, [_envelope(_message("start"))], home=home, db=db)
    assert launched.delivery is not None
    thread_id = launched.delivery.thread_id

    first = transport.send(thread_id, "working on it", event_id="run-1-final", db=db)
    again = transport.send(thread_id, "working on it", event_id="run-1-final", db=db)

    assert first.id == again.id, "a retried send opened a second turn"
    assert str(first.role) == "assistant"
    roles = [str(m.role) for m in store.messages(thread_id, db=db)]
    assert roles.count("assistant") == 1

    posts = api.posts()
    assert len(posts) == 1, "a retried send posted a second message"
    assert posts[0]["payload"] == {"channel": CHANNEL, "text": "working on it"}
    assert posts[0]["headers"]["authorization"] == f"Bearer {TOKEN}"


def test_send_into_a_thread_posts_with_the_thread_ts(
    home: Path, db: str, launches, api
) -> None:
    """A reply in a Slack thread posts into that thread, not the channel root."""
    transport = _transport()
    event = _message("hello", thread_ts="1700000000.000050")
    launched, _acks = _socket_tick(transport, [_envelope(event)], home=home, db=db)
    assert launched.delivery is not None

    transport.send(launched.delivery.thread_id, "reply", db=db)

    posts = api.posts()
    assert posts[-1]["payload"] == {
        "channel": CHANNEL,
        "text": "reply",
        "thread_ts": "1700000000.000050",
    }


def test_send_refuses_an_unmappable_thread(home: Path, db: str) -> None:
    """An unknown thread id is an error, never a post to a guessed channel."""
    with pytest.raises(slack.SlackError):
        _transport().send("slack-nope", "hello", db=db)


def test_api_errors_name_the_method_and_status_not_the_token(
    monkeypatch, home: Path, db: str
) -> None:
    """A failed Web API call reports the method and status, never the credential."""
    store.open_thread(
        slack.thread_key("slack", CHANNEL, None),
        origin="chat",
        origin_ref=f"slack:{CHANNEL}",
        db=db,
    )

    def boom(request, timeout=None):  # noqa: ARG001 — timeout is the API's
        raise urllib.error.HTTPError(getattr(request, "full_url", ""), 403, "Forbidden", {}, None)

    monkeypatch.setattr(slack.urllib.request, "urlopen", boom)

    with pytest.raises(slack.SlackError) as caught:
        _transport().send(slack.thread_key("slack", CHANNEL, None), "hi", db=db)

    message = str(caught.value)
    assert "chat.postMessage" in message
    assert "403" in message
    assert TOKEN not in message


# -- registration ----------------------------------------------------------


def test_import_is_inert_and_registers_slack() -> None:
    """``import mini_ork.integrations`` names both transports and opens nothing."""
    import mini_ork.integrations as integrations

    assert integrations.SlackTransport is SlackTransport
    assert integrations.TelegramTransport is TelegramTransport
    assert integrations.thread_key("slack", "C1", None) == "slack-C1"
    # A sub-thread is part of the key; the top level is not.
    assert integrations.thread_key("slack", "C1", "1.2") == "slack-C1-1_2"
