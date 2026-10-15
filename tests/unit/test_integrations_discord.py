"""Tests for ``mini_ork.integrations.discord`` — both tiers, offline.

No network, no TLS, no ``cryptography`` and no run is ever spawned. The Gateway
tier is driven through the ``_ws.connect`` seam with a scripted connection (the
same escape hatch :mod:`mini_ork.integrations._ws` documents for tests and that
``test_integrations_slack.py`` uses for its socket), ``urllib.request.urlopen``
is monkeypatched so ``GET /gateway/bot`` mints a URL and
``POST /channels/{id}/messages`` records instead of posting, the vault is a fake
resolved by key name, and ``control.launch_run`` / ``control.steer_run`` are
stubbed with the fixture shape ``tests/unit/test_integrations_telegram.py``
established. The threads DB is a real ``state.db`` under ``tmp_path``: the store
creates its own schema on first write, so no migration step is needed.

What is pinned:

* importing the module needs nothing — no secret, no ``cryptography``;
* ``GET /gateway/bot`` carries the bot token, the URL it answers with is handed
  to ``_ws.connect`` (there is no second WebSocket client), IDENTIFY carries
  ``GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT`` and the token, and the
  heartbeat carries the last sequence number the gateway sent;
* the first message from a channel opens the thread ``("discord", channel)``
  names and *launches*; a follow-up on the same key *steers* or *continues* and
  never launches a second run, while a different channel is a different thread;
* a bot's own message is ignored, and a replayed message id is a *duplicate* —
  the transcript gains one row, not two;
* a gateway that closes with its heartbeat unanswered is *reported* (and the
  socket closed), and a busy-then-silent gateway is never retried in a loop;
* ``send`` posts to the channel the thread's record names, projects the turn
  onto the transcript exactly once even on a retry, and refuses an unmappable
  thread id;
* the Interactions tier verifies Ed25519 over ``X-Signature-Timestamp + raw
  body``, answers a PING with a PONG, refuses a forged signature and a missing
  header, and delivers a command's option text through the same seam;
* without ``cryptography`` the webhook *refuses* and names
  ``pip install 'mini-ork[integrations]'`` — it never accepts unverified input;
* both vault keys fail closed, naming the key, and no secret reaches
  ``__repr__`` or an exception message.

The stub ``cryptography`` below decides "valid" from a hash over the public key
and the signed bytes rather than from curve math, because what this suite pins
is the *wiring* — the header lookup, the signed base, verify-then-parse, the
refusal. The real Ed25519 arithmetic is pinned by the run's blind spec suite,
which anchors its own signer to RFC 8032.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from mini_ork.integrations import _ws  # noqa: E402
from mini_ork.integrations import discord as discord_integration  # noqa: E402
from mini_ork.integrations.discord import DiscordTransport  # noqa: E402
from mini_ork.threads import store  # noqa: E402

#: The wss URL the faked ``GET /gateway/bot`` answers with.
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

#: GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT, as IDENTIFY sends them.
INTENTS = (1 << 9) | (1 << 12) | (1 << 15)

#: Discord's own documented heartbeat interval, in milliseconds — the real
#: value, so the parsed cadence in a report reads as it would in production.
HEARTBEAT_MS = 41250

BOT_TOKEN = "discord-test-bot-token-not-a-real-one"
PUBLIC_KEY_HEX = "11" * 32
OTHER_KEY_HEX = "22" * 32


def state_db(home: Path) -> str:
    return str(Path(home) / "state.db")


# -- fakes -----------------------------------------------------------------


class FakeVault:
    """Resolves by key name and never returns ``""`` — the ``vault_environment`` shape."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = dict(values)

    def resolve(self, key: str) -> str | None:
        return self._values.get(key)

    def describe(self) -> str:
        return "test fake vault"


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeHTTP:
    """Records requests; answers the two endpoints the transport may call."""

    def __init__(self, gateway_body: bytes | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        if gateway_body is None:
            gateway_body = json.dumps({"url": GATEWAY_URL, "shards": 1}).encode()
        self._gateway_body = bytes(gateway_body)

    def __call__(self, request: Any, timeout: float | None = None, *args: Any, **kwargs: Any):
        if isinstance(request, (str, bytes)):
            url = str(request)
            method = "POST" if kwargs.get("data") is not None else "GET"
            headers: dict[str, str] = {}
            body = kwargs.get("data")
        else:
            url = str(getattr(request, "full_url", ""))
            get_method = getattr(request, "get_method", None)
            method = str(get_method() if get_method else "GET").upper()
            headers = {}
            items = getattr(request, "header_items", None)
            for key, value in (items() if items else []):
                headers[str(key).lower()] = str(value)
            body = getattr(request, "data", kwargs.get("data"))
        self.requests.append(
            {"method": method, "url": url, "headers": headers, "body": body}
        )
        path = urllib.parse.urlsplit(url).path or "/"
        if path.endswith("/gateway/bot"):
            return FakeResponse(self._gateway_body)
        if method == "POST" and path.endswith("/messages"):
            return FakeResponse(b'{"id": "999"}')
        raise urllib.error.URLError(f"test fake http: unexpected request {method} {path}")

    def with_path(self, suffix: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if (urllib.parse.urlsplit(r["url"]).path or "").endswith(suffix)]

    def authorization_values(self) -> list[str]:
        return [
            value
            for record in self.requests
            for key, value in record["headers"].items()
            if key.lower() == "authorization"
        ]


class FakeGateway:
    """A scripted Gateway handed out through the patched ``_ws.connect``.

    Reads and writes are capped the way a spinner would blow them: a transport
    that reads or writes more than ``LIMIT`` times in one tick fails its test
    instead of hanging.
    """

    LIMIT = 200

    def __init__(self, incoming: list[str]) -> None:
        self.incoming = list(incoming)
        self.sent: list[dict[str, Any]] = []
        self.seq_at_send: list[Any] = []
        self.last_seq_delivered: Any = None
        self.connect_calls = 0
        self.connect_url: str | None = None
        self.closed = False

    def recv_message(self) -> str:
        if not self.incoming:
            raise _ws.WebSocketClosed(1000, "script exhausted")
        text = self.incoming.pop(0)
        envelope = json.loads(text)
        if isinstance(envelope, dict) and envelope.get("s") is not None:
            self.last_seq_delivered = envelope["s"]
        return text

    def send_text(self, text: str) -> None:
        self.seq_at_send.append(self.last_seq_delivered)
        self.sent.append(json.loads(text))

    def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True

    def sent_ops(self, op: int) -> list[dict[str, Any]]:
        return [e for e in self.sent if isinstance(e, dict) and e.get("op") == op]

    def assert_heartbeats_carry_last_sequence(self) -> None:
        for index, envelope in enumerate(self.sent):
            if not (isinstance(envelope, dict) and envelope.get("op") == 1):
                continue
            expected = self.seq_at_send[index]
            assert envelope.get("d") == expected, (
                f"a heartbeat carried d={envelope.get('d')!r}; the last "
                f"sequence number the gateway had sent was {expected!r}"
            )


class GatewayHolder:
    """Rewriteable script: the patched ``_ws.connect`` hands out the current one."""

    def __init__(self) -> None:
        self.current: FakeGateway | None = None

    def rewrite(self, incoming: list[str]) -> FakeGateway:
        self.current = FakeGateway(incoming)
        return self.current


class FakeControl:
    """``launch_run``/``steer_run`` that record instead of spawning."""

    def __init__(self, home: Path) -> None:
        self.launches: list[dict[str, Any]] = []
        self.steers: list[dict[str, Any]] = []
        self._home = Path(home)

    def launch_run(self, home: Any, recipe: Any, kickoff: Any, run_id: Any = None) -> dict[str, Any]:
        self.launches.append({"home": str(home), "recipe": recipe, "run_id": run_id})
        return {
            "ok": True,
            "kickoff_path": str(self._home / "kickoff.md"),
            "log_path": str(self._home / "run.log"),
        }

    def steer_run(
        self, db: Any, run_id: Any, text: Any, source: Any = None, severity: Any = None
    ) -> dict[str, Any]:
        self.steers.append({"run_id": run_id, "text": text, "source": source})
        return {"ok": True}


# -- envelope builders -----------------------------------------------------


def hello(interval: int = HEARTBEAT_MS) -> str:
    return json.dumps({"t": None, "s": None, "op": 10, "d": {"heartbeat_interval": interval}})


def dispatch(event_type: str, seq: int, data: dict[str, Any]) -> str:
    return json.dumps({"t": event_type, "s": seq, "op": 0, "d": data})


def message_create(
    seq: int, message_id: str, channel_id: str, content: str, *, bot: bool = False
) -> str:
    return dispatch(
        "MESSAGE_CREATE",
        seq,
        {
            "id": message_id,
            "channel_id": channel_id,
            "author": {"id": "42", "username": "test", "bot": bot},
            "content": content,
        },
    )


# -- stub cryptography -----------------------------------------------------


def signed_base(timestamp: str, body: bytes) -> bytes:
    """``timestamp + raw body`` — the bytes Discord signs."""
    return timestamp.encode("utf-8") + body


def sign(key_hex: str, timestamp: str, body: bytes) -> str:
    """The stub's signature: a hash over the key and the signed bytes."""
    return hashlib.sha256(bytes.fromhex(key_hex) + signed_base(timestamp, body)).hexdigest()


def install_stub_cryptography(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stand-in package with ``cryptography``'s import shape, not its curve math."""
    InvalidSignature = type("InvalidSignature", (Exception,), {})

    class Ed25519PublicKey:
        def __init__(self, raw: bytes) -> None:
            self._raw = bytes(raw)

        @classmethod
        def from_public_bytes(cls, data: bytes) -> Ed25519PublicKey:
            if not isinstance(data, (bytes, bytearray)) or len(data) != 32:
                raise ValueError("test stub: an Ed25519 public key is 32 bytes")
            return cls(data)

        def verify(self, signature: bytes, data: bytes) -> None:
            if bytes(signature) != hashlib.sha256(self._raw + bytes(data)).digest():
                raise InvalidSignature("test stub: signature did not verify")

    crypto = types.ModuleType("cryptography")
    crypto.__path__ = []
    exceptions = types.ModuleType("cryptography.exceptions")
    exceptions.InvalidSignature = InvalidSignature
    hazmat = types.ModuleType("cryptography.hazmat")
    hazmat.__path__ = []
    primitives = types.ModuleType("cryptography.hazmat.primitives")
    primitives.__path__ = []
    asymmetric = types.ModuleType("cryptography.hazmat.primitives.asymmetric")
    asymmetric.__path__ = []
    ed25519 = types.ModuleType("cryptography.hazmat.primitives.asymmetric.ed25519")
    ed25519.Ed25519PublicKey = Ed25519PublicKey

    crypto.exceptions = exceptions
    crypto.hazmat = hazmat
    hazmat.primitives = primitives
    primitives.asymmetric = asymmetric
    asymmetric.ed25519 = ed25519
    for name, module in (
        ("cryptography", crypto),
        ("cryptography.exceptions", exceptions),
        ("cryptography.hazmat", hazmat),
        ("cryptography.hazmat.primitives", primitives),
        ("cryptography.hazmat.primitives.asymmetric", asymmetric),
        ("cryptography.hazmat.primitives.asymmetric.ed25519", ed25519),
    ):
        monkeypatch.setitem(sys.modules, name, module)


def block_cryptography(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every ``cryptography`` import with the ``None`` sys.modules sentinel."""
    for name in [n for n in list(sys.modules) if n == "cryptography" or n.startswith("cryptography.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "cryptography", None)


# -- fixtures --------------------------------------------------------------


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch):
    """Patch the vault the transport resolves through; return a setter."""

    def install(values: dict[str, str]) -> FakeVault:
        fake = FakeVault(values)
        monkeypatch.setattr(discord_integration, "vault_environment", lambda: fake)
        return fake

    return install


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home directory whose ``state.db`` the store creates on first write."""
    h = tmp_path / "home"
    h.mkdir(parents=True)
    return h


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch):
    """Intercept urllib for both import styles (``urllib.request.urlopen``)."""

    def install(gateway_body: bytes | None = None) -> FakeHTTP:
        fake = FakeHTTP(gateway_body=gateway_body)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        return fake

    return install


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> GatewayHolder:
    """Hand ``_ws.connect`` the scripted connection instead of opening one."""
    holder = GatewayHolder()
    original = _ws.connect

    def fake_connect(url: str, *args: Any, **kwargs: Any) -> FakeGateway:
        assert holder.current is not None, "no gateway script was installed"
        assert original is not None  # the real opener is only shadowed, never called
        holder.current.connect_calls += 1
        holder.current.connect_url = url
        return holder.current

    monkeypatch.setattr(_ws, "connect", fake_connect)
    return holder


@pytest.fixture
def control(monkeypatch: pytest.MonkeyPatch, home: Path) -> FakeControl:
    """Stub the run control, per the threads-service's documented test pattern."""
    import mini_ork.web.control as web_control

    fake = FakeControl(home)
    monkeypatch.setattr(web_control, "launch_run", fake.launch_run)
    monkeypatch.setattr(web_control, "steer_run", fake.steer_run)
    return fake


def make_transport(vault, **overrides: Any) -> DiscordTransport:
    values = dict(overrides.pop("vault_values", {"DISCORD_BOT_TOKEN": BOT_TOKEN, "DISCORD_PUBLIC_KEY": PUBLIC_KEY_HEX}))
    vault(values)
    return DiscordTransport(**overrides)


def webhook_headers(
    key_hex: str, timestamp: str, body: bytes, *, signature: str | None = None
) -> dict[str, str]:
    return {
        "X-Signature-Ed25519": signature if signature is not None else sign(key_hex, timestamp, body),
        "X-Signature-Timestamp": timestamp,
    }


# =====================================================================
# packaging, registration, import
# =====================================================================


def test_import_needs_no_secret_and_no_cryptography():
    """Importing the module is inert: no vault, no extra, no network."""
    exe = shutil.which("python3.11") or sys.executable
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(REPO)}
    proc = subprocess.run(
        [
            exe,
            "-c",
            "import sys; import mini_ork.integrations.discord as d; "
            "print(d.NAME); print('cryptography' in sys.modules)",
        ],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["discord", "False"], (
        "importing the module pulled cryptography (or named itself something "
        f"else): {proc.stdout!r}"
    )


def test_registered_in_the_package_and_the_extra_is_declared():
    import mini_ork.integrations as package

    assert package.DiscordTransport is DiscordTransport, (
        "mini_ork.integrations does not re-export the discord module's class"
    )
    assert package.DISCORD_BOT_TOKEN_VAULT_KEY == "DISCORD_BOT_TOKEN"
    assert package.DISCORD_PUBLIC_KEY_VAULT_KEY == "DISCORD_PUBLIC_KEY"

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    names = sorted(
        entry.split(">")[0].split("=")[0].split("<")[0].split("!")[0].split("[")[0].strip().lower()
        for entry in pyproject["project"]["dependencies"]
    )
    assert names == ["jsonschema", "pyyaml"], (
        f"the required runtime dependencies moved: {pyproject['project']['dependencies']!r}"
    )
    extra = pyproject["project"]["optional-dependencies"]["integrations"]
    assert any(entry.startswith("cryptography") and ">=42" in entry for entry in extra), (
        f"the integrations extra does not pin cryptography>=42: {extra!r}"
    )


# =====================================================================
# simple tier — the Gateway
# =====================================================================


def test_gateway_hello_identifies_and_launches_the_first_message(
    http, gateway, control, vault, home
):
    http = http()
    script = gateway.rewrite(
        [
            hello(),
            dispatch("READY", 1, {"v": 10}),
            message_create(2, "111", "C1", "hello mini-ork"),
        ]
    )
    transport = make_transport(vault)
    result = transport.poll_once(home=home, limit=8)

    # GET /gateway/bot with the bot token over urllib
    calls = http.with_path("/gateway/bot")
    assert len(calls) == 1 and calls[0]["method"] == "GET"
    assert any(BOT_TOKEN in value for value in http.authorization_values()), (
        "no Authorization header carried the bot token"
    )

    # the answer's URL goes to _ws.connect — no second WebSocket client
    assert script.connect_calls == 1
    assert script.connect_url == GATEWAY_URL

    # IDENTIFY carries the three intents and the token
    identify = script.sent_ops(2)[0]["d"]
    assert identify["intents"] == INTENTS
    assert BOT_TOKEN in identify["token"]

    # HELLO's heartbeat carries the last sequence number the gateway sent
    assert script.sent_ops(1), "HELLO started no heartbeat"
    script.assert_heartbeats_carry_last_sequence()

    # the message launched a chat thread keyed on its channel
    assert [launch["run_id"] for launch in control.launches] == ["discord-C1"]
    assert result.delivery is not None and result.delivery.action == "launched"
    thread = store.get_thread("discord-C1", db=state_db(home))
    assert thread is not None and str(thread.origin) == "chat"
    texts = [m.text for m in store.messages("discord-C1", db=state_db(home))]
    assert "hello mini-ork" in texts


def test_gateway_second_message_steers_or_continues_never_relaunches(
    http, gateway, control, vault, home
):
    http()
    transport = make_transport(vault)

    gateway.rewrite([hello(), message_create(2, "111", "C1", "first")])
    first = transport.poll_once(home=home, limit=8)
    gateway.rewrite([hello(), message_create(3, "222", "C1", "second")])
    second = transport.poll_once(home=home, limit=8)

    assert first.delivery is not None and first.delivery.action == "launched"
    assert second.delivery is not None
    assert second.delivery.action in ("steered", "continued"), (
        f"a second message in the same conversation reported {second.delivery.action!r}"
    )
    assert second.delivery.thread_id == first.delivery.thread_id
    assert len(control.launches) == 1, "a second run was launched for one conversation"
    if second.delivery.action == "steered":
        assert control.steers, "'steered' was reported but nothing was steered"


def test_gateway_distinct_channels_are_distinct_threads(http, gateway, control, vault, home):
    http()
    transport = make_transport(vault)

    gateway.rewrite([hello(), message_create(2, "111", "C1", "in channel")])
    transport.poll_once(home=home, limit=8)
    gateway.rewrite([hello(), message_create(3, "222", "T1", "in a thread")])
    transport.poll_once(home=home, limit=8)

    keys = [launch["run_id"] for launch in control.launches]
    assert keys == ["discord-C1", "discord-T1"], (
        f"a channel and a thread inside it collapsed onto one thread: {keys!r}"
    )


def test_gateway_bot_author_is_ignored(http, gateway, control, vault, home):
    http()
    script = gateway.rewrite([hello(), message_create(2, "333", "C1", "beep", bot=True)])
    transport = make_transport(vault)
    result = transport.poll_once(home=home, limit=8)

    assert result.action == "ignored", "a bot-authored message was acted on"
    assert control.launches == []
    assert store.list_threads(db=state_db(home)) == []
    assert script.closed, "the tick did not close its connection"


def test_gateway_replayed_message_id_is_a_duplicate(http, gateway, control, vault, home):
    http()
    transport = make_transport(vault)
    envelope = message_create(2, "111", "C1", "hello once")

    gateway.rewrite([hello(), envelope])
    first = transport.poll_once(home=home, limit=8)
    gateway.rewrite([hello(), envelope])
    second = transport.poll_once(home=home, limit=8)

    assert first.delivery is not None and first.delivery.action == "launched"
    assert second.delivery is not None and second.delivery.action == "duplicate", (
        f"a replayed message id reported {second.delivery.action!r}"
    )
    assert len(control.launches) == 1 and control.steers == []
    user_texts = [
        m.text
        for m in store.messages("discord-C1", db=state_db(home))
        if str(m.role) == "user"
    ]
    assert user_texts.count("hello once") == 1


def test_gateway_missed_heartbeat_ack_is_reported_and_closes(http, gateway, control, vault, home):
    """The gateway closes on a beat it never answered (no op 11): report it."""
    http()
    script = gateway.rewrite([hello(), dispatch("READY", 1, {"v": 10})])
    transport = make_transport(vault)

    with pytest.raises(discord_integration.DiscordError) as caught:
        transport.poll_once(home=home, limit=8)

    assert script.sent_ops(1), "no heartbeat was ever sent to miss an ACK for"
    assert script.closed, "the connection was not closed on the missed ACK"
    assert "41.25s" in str(caught.value), (
        f"the report does not name the cadence HELLO asked for: {caught.value}"
    )
    assert control.launches == []


def test_gateway_reads_a_bounded_number_of_frames(http, gateway, control, vault, home):
    """Neither reads nor writes spin: a tick is capped by its frame budget."""
    http()
    script = gateway.rewrite(
        [hello()] + [dispatch("PRESENCE_UPDATE", seq, {}) for seq in range(1, 40)]
    )
    transport = make_transport(vault)
    result = transport.poll_once(home=home, limit=4)

    assert result.action == "ignored"
    assert len(script.incoming) == 40 - 4, (
        f"the tick read {40 - len(script.incoming)} frames for a budget of 4"
    )


# =====================================================================
# outbound — send()
# =====================================================================


def test_send_posts_to_the_threads_channel(http, gateway, control, vault, home):
    http = http()
    transport = make_transport(vault)
    gateway.rewrite([hello(), message_create(2, "111", "C1", "hello")])
    transport.poll_once(home=home, limit=8)

    transport.send("discord-C1", "the answer", home=home)

    posts = http.with_path("/channels/C1/messages")
    assert len(posts) == 1, f"send() issued {len(posts)} POSTs: {http.requests!r}"
    assert posts[0]["method"] == "POST"
    assert any(BOT_TOKEN in value for value in http.authorization_values())
    assert json.loads(bytes(posts[0]["body"]).decode()) == {"content": "the answer"}
    roles = [(str(m.role), m.text) for m in store.messages("discord-C1", db=state_db(home))]
    assert ("assistant", "the answer") in roles, (
        f"the turn never reached the transcript: {roles!r}"
    )


def test_send_refuses_a_thread_it_cannot_map(http, gateway, control, vault, home):
    http()
    transport = make_transport(vault)
    with pytest.raises(discord_integration.DiscordError):
        transport.send("nobody-knows-this-thread", "hello?", home=home)


def test_send_is_a_no_op_on_a_retried_event_id(http, gateway, control, vault, home):
    http = http()
    transport = make_transport(vault)
    gateway.rewrite([hello(), message_create(2, "111", "C1", "hello")])
    transport.poll_once(home=home, limit=8)

    transport.send("discord-C1", "the answer", event_id="turn-1", home=home)
    transport.send("discord-C1", "the answer", event_id="turn-1", home=home)

    posts = http.with_path("/channels/C1/messages")
    assert len(posts) == 1, f"a retried send posted the turn twice: {http.requests!r}"
    assistant = [
        m.text
        for m in store.messages("discord-C1", db=state_db(home))
        if str(m.role) == "assistant"
    ]
    assert assistant == ["the answer"]


# =====================================================================
# secrets — fail closed, never rendered
# =====================================================================


def test_missing_bot_token_names_the_key_and_sends_nothing(http, vault, home):
    http = http()
    vault({"DISCORD_PUBLIC_KEY": PUBLIC_KEY_HEX})
    with pytest.raises(discord_integration.DiscordConfigError) as caught:
        DiscordTransport()
    assert "DISCORD_BOT_TOKEN" in str(caught.value)
    assert http.requests == [], "a request left before the missing key failed the transport"


def test_missing_public_key_names_the_key(monkeypatch, http, vault, home):
    install_stub_cryptography(monkeypatch)
    http()
    transport = make_transport(vault, vault_values={"DISCORD_BOT_TOKEN": BOT_TOKEN})
    body = b'{"type": 1}'
    with pytest.raises(discord_integration.DiscordConfigError) as caught:
        transport.handle_webhook(webhook_headers(PUBLIC_KEY_HEX, "1800000000", body), body, home=home)
    assert "DISCORD_PUBLIC_KEY" in str(caught.value)
    assert store.list_threads(db=state_db(home)) == []


def test_no_token_in_repr_or_in_an_exception(http, vault, home):
    http = http(gateway_body=json.dumps({"url": "", "shards": 1}).encode())
    transport = make_transport(vault)
    assert BOT_TOKEN not in repr(transport)
    assert "***" in repr(transport)
    with pytest.raises(discord_integration.DiscordError) as caught:
        transport.poll_once(home=home, limit=8)
    assert BOT_TOKEN not in str(caught.value), "the token leaked through an exception message"


# =====================================================================
# secure tier — the Interactions webhook
# =====================================================================


def test_webhook_ping_is_answered_with_pong_and_delivers_nothing(monkeypatch, http, control, vault, home):
    install_stub_cryptography(monkeypatch)
    http()
    transport = make_transport(vault)
    body = b'{"type": 1}'

    answer = transport.handle_webhook(webhook_headers(PUBLIC_KEY_HEX, "1800000000", body), body, home=home)

    assert isinstance(answer, dict) and answer.get("type") == 1, (
        f"a signed PING was not answered with a type-1 PONG: {answer!r}"
    )
    assert control.launches == []
    assert store.list_threads(db=state_db(home)) == []


def test_webhook_refuses_a_forged_signature_and_a_missing_one(monkeypatch, http, control, vault, home):
    install_stub_cryptography(monkeypatch)
    http()
    transport = make_transport(vault)
    body = json.dumps({"type": 2, "id": "990", "channel_id": "C9", "data": {"name": "mini"}}).encode()

    forged = webhook_headers(PUBLIC_KEY_HEX, "1800000000", body, signature=sign(OTHER_KEY_HEX, "1800000000", body))
    with pytest.raises(discord_integration.DiscordSignatureError):
        transport.handle_webhook(forged, body, home=home)

    unsigned = {"X-Signature-Timestamp": "1800000000"}
    with pytest.raises(discord_integration.DiscordSignatureError):
        transport.handle_webhook(unsigned, body, home=home)

    assert control.launches == []
    assert store.list_threads(db=state_db(home)) == []


def test_webhook_delivers_a_signed_command_interaction(monkeypatch, http, control, vault, home):
    install_stub_cryptography(monkeypatch)
    http()
    transport = make_transport(vault)
    body = json.dumps(
        {
            "type": 2,
            "id": "991",
            "token": "interaction-token-not-a-real-one",
            "channel_id": "C9",
            "user": {"id": "7", "bot": False},
            "data": {
                "name": "mini",
                "options": [{"name": "text", "type": 3, "value": "run the spec suite"}],
            },
        }
    ).encode()

    transport.handle_webhook(webhook_headers(PUBLIC_KEY_HEX, "1800000000", body), body, home=home)

    assert [launch["run_id"] for launch in control.launches] == ["discord-C9"]
    texts = [m.text for m in store.messages("discord-C9", db=state_db(home))]
    assert "run the spec suite" in texts


def test_webhook_without_cryptography_refuses_and_names_the_extra(monkeypatch, http, control, vault, home):
    block_cryptography(monkeypatch)
    http()
    transport = make_transport(vault)
    body = b'{"type": 1}'

    with pytest.raises(discord_integration.DiscordConfigError) as caught:
        transport.handle_webhook(webhook_headers(PUBLIC_KEY_HEX, "1800000000", body), body, home=home)

    message = str(caught.value)
    assert "pip install" in message and "mini-ork[integrations]" in message, (
        f"the refusal does not name the extra: {message}"
    )
    assert control.launches == []
    assert store.list_threads(db=state_db(home)) == []
