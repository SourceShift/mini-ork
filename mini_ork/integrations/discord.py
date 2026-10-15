"""Discord, both tiers: the Gateway for a laptop and an Interactions webhook for a team.

The split is the one :mod:`mini_ork.integrations.slack` and
:mod:`mini_ork.integrations.telegram` draw, reached Discord's way. The *simple*
tier is :meth:`DiscordTransport.poll_once`: ``GET /gateway/bot`` with the bot
token over stdlib ``urllib``, then the ``wss`` URL it answers with is handed to
:mod:`mini_ork.integrations._ws`. No public URL, no server, no inbound port —
just a bot token and outbound TLS, which is what a person trying mini-ork on a
laptop can actually configure. The *secure* tier is
:meth:`DiscordTransport.handle_webhook`: an inbound port and Discord's own
Ed25519 ``X-Signature-Ed25519`` over ``X-Signature-Timestamp + raw body``, which
is the only way Discord will talk to an endpoint at all. Neither tier is a toy
of the other; what differs is transport mechanics, while what matters — which
thread a message lands on — is shared.

**The wire format is not re-implemented here.** The RFC 6455 client is
:mod:`mini_ork.integrations._ws`, hand-written and stdlib-only for the reason
that module documents, and this transport only asks it to ``connect``,
``recv_message`` and ``send_text`` — the same three calls the Slack transport
makes, over the same seam (``sock=``) that makes a fake connection possible.
The Gateway is a push stream, so what this module owns is Discord's
*vocabulary*: op 10 HELLO carries the heartbeat interval, op 1 heartbeats carry
the last sequence number, op 2 IDENTIFY carries the intents and the token, and
``MESSAGE_CREATE`` dispatches carry the message. All of them are constants here
because each one has to be greppable.

**``cryptography`` is the one optional dependency, and it is lazy.** Ed25519 is
not ``hmac``; a signature scheme worth using is worth not hand-rolling, so the
secure tier calls ``cryptography``'s ``Ed25519PublicKey``. ``pyproject.toml``'s
runtime dependencies are ``PyYAML`` and ``jsonschema`` and this module does not
move them: the package lives behind a new ``integrations`` extra and is imported
*inside* the verification path, so ``import mini_ork.integrations.discord``
succeeds on a machine that has neither the extra nor a secret configured. When
the import fails the webhook **refuses** — it names ``pip install
'mini-ork[integrations]'`` and accepts nothing — because a signature check that
falls back to "trust me" is not a degraded mode, it is an open endpoint.

**The two secrets fail closed.** ``DISCORD_BOT_TOKEN`` is resolved eagerly, the
way the Slack and Telegram tokens are: a transport that cannot authenticate
should fail at construction, not on the first tick of a loop that will retry
forever. ``DISCORD_PUBLIC_KEY`` is resolved lazily and by key name, because a
Gateway-only deployment never verifies a signature and asking the vault for a
key that was never configured would turn a working simple tier into a startup
failure. Fail-closed whenever the key *is* asked for, for the reason
:func:`_resolve_fail_closed` spells out. No key ever reaches ``__repr__``, a log
line, or an exception message — the token rides an ``Authorization: Bot …``
header and the public key is public, but the habit is the point.

**The thread is the state machine's, not this module's.** Inbound goes through
:func:`mini_ork.threads.service.deliver` and nothing else, so the four outcomes
(``launched``/``steered``/``continued``/``duplicate``) are the ones the canvas
and work-intake already share, and a redelivered event — a gateway resume, an
interaction Discord retries because the first answer was slow — is deduped by
``external_id`` (``discord:{message_id}``, ``discord:{interaction_id}``) rather
than by a cache this module would have to keep warm. That dedupe is also why the
webhook needs no replay window of its own: the timestamp is part of the signed
message, and a replay is a ``duplicate``, not a second run. Outbound is
:meth:`DiscordTransport.send`: it projects the turn onto the thread's transcript
via :func:`mini_ork.threads.store.append_message` — the transport-agnostic seam
a canvas and Discord both read — and then posts it with ``POST
/channels/{channel_id}/messages``, so the transcript write is what makes a
retried send a no-op instead of a second message on the wire.

**A bounded tick, and a heartbeat that must be answered.** :meth:`poll_once` is
one connection and a frame budget: it drains the gateway until a message event
arrives (the tick has something to report) or the peer closes, and then it
closes the socket itself. One connection per tick is the same shape the Slack
tier has, and it is why exactly one heartbeat is due per tick: HELLO is answered
with IDENTIFY and then op 1 carrying the last sequence number Discord sent
(``None`` until a dispatch gives one, which is Discord's own "I have seen
nothing yet"). What is never left to the caller is a **zombie**: a peer that
closes on us with that heartbeat still unanswered never confirmed it read us, so
the transport closes the socket and *reports* it — :class:`DiscordError`, as a
value the caller can act on — instead of reconnecting inside the tick or
spinning. A tick that finished its work (a message arrived) closed the socket
itself and is not a zombie.

**What is deliberately missing.** No resume/reconnect (op 6/op 7), no
rate-limit backoff, no slash-command registration, no presence/voice intents,
no file uploads, and no ``ws://`` tier (``_ws`` refuses it). Each is a real
feature of the platform and none is a feature of the conversation seam the
transports exist to be; adding one later is an edit here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mini_ork.dispatch.secrets import SecretStoreError
from mini_ork.dispatch.vault import vault_environment
from mini_ork.intake import webhooks
from mini_ork.threads import service, store
from mini_ork.threads.model import MessageRole, ThreadMessage, ThreadOrigin
from mini_ork.threads.service import Delivery

from . import _ws
from ._base import PollResult, merge_poll_results, thread_key

#: The transport's id, the first component of every :func:`thread_key` it mints,
#: and the prefix of every ``external_id`` and ``origin_ref`` it writes. One
#: string, one meaning.
NAME = "discord"

#: The vault keys the two secrets are resolved from, named exactly as the
#: deployment writes them. Named rather than inlined because the error message
#: echoes the key: "which key is missing" is the whole diagnostic a person gets,
#: and it has to match the name they wrote in their vault.
BOT_TOKEN_VAULT_KEY = "DISCORD_BOT_TOKEN"
PUBLIC_KEY_VAULT_KEY = "DISCORD_PUBLIC_KEY"

#: Where the Discord API lives — ``/api/v10`` is this module's ``v0``, and a
#: version bump is an edit here rather than a scattering. Overridable so a test
#: — or a proxy in front of Discord — never has to reach discord.com.
DEFAULT_API_BASE = "https://discord.com/api/v10"

#: The two headers a signed interaction carries. Discord's names, verbatim; the
#: value of the first is the hex Ed25519 signature over
#: ``timestamp + raw body``, and the second is a string, not a number, because
#: it is signed as text.
SIGNATURE_HEADER = "x-signature-ed25519"
TIMESTAMP_HEADER = "x-signature-timestamp"

#: The one dependency this transport does not ship, named the way the message
#: that refuses an unverifiable webhook names it. ``mini-ork`` is the
#: distribution; ``mini_ork`` is the package, and a person pastes what pip takes.
EXTRA_INSTALL = "pip install 'mini-ork[integrations]'"

#: Gateway opcodes, as the kickoff names them. Constants because every one of
#: them appears in a predicate a reader has to be able to grep.
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

#: Gateway intents: guild messages, direct messages, and their content.
#: ``MESSAGE_CONTENT`` is privileged in Discord's account settings and a bot
#: that does not have it enabled receives message events with an empty
#: ``content`` — which is why the transport reads the text rather than assuming
#: it and reports an empty one as nothing to deliver.
INTENT_GUILD_MESSAGES = 1 << 9
INTENT_DIRECT_MESSAGES = 1 << 12
INTENT_MESSAGE_CONTENT = 1 << 15
INTENTS = INTENT_GUILD_MESSAGES | INTENT_DIRECT_MESSAGES | INTENT_MESSAGE_CONTENT

#: Interaction types. PING is Discord's endpoint handshake and is answered with
#: PONG (the same integer, which is Discord's own convention and not a typo);
#: the other two are the ones whose text is a turn on a thread.
INTERACTION_PING = 1
INTERACTION_APPLICATION_COMMAND = 2
INTERACTION_MESSAGE_COMPONENT = 3
PONG_TYPE = 1

#: The dispatch the simple tier delivers. A dispatch of any other name is
#: acknowledged by being read and reported as ``ignored``.
MESSAGE_CREATE_TYPE = "MESSAGE_CREATE"

#: How many frames one tick reads before it stops waiting for a message. Not a
#: rate limit and not a nicety: a live gateway streams presence and membership
#: dispatches this transport ignores, and a tick that only ended when the peer
#: closed would read forever without ever returning to its caller.
DEFAULT_FRAME_BUDGET = 16

#: How long the gateway read and the REST call may block when the caller does
#: not say. A gateway that goes silent mid-read must fail the caller rather than
#: pin the thread running it.
DEFAULT_WS_TIMEOUT = 30.0
DEFAULT_HTTP_TIMEOUT = 30.0

#: What HELLO is assumed to mean when it omits ``heartbeat_interval`` entirely.
#: Discord always sends it; this is the value in its documentation, and it is
#: better to heartbeat on a guessed interval than to never heartbeat at all.
DEFAULT_HEARTBEAT_SECONDS = 41.25

#: The floor under HELLO's interval. Discord sends it in *milliseconds* and its
#: documented value is ``41250`` — 41.25 s. The floor is for a degenerate value
#: (a test-sized ``30``, a malformed one): paced that tightly, the "next beat is
#: due with the previous one unacknowledged" check below would fire inside a
#: single fast tick and report a zombie that does not exist. One second is far
#: below Discord's own minimum and far above a tick.
MIN_HEARTBEAT_SECONDS = 1.0


class DiscordError(RuntimeError):
    """A Discord transport could not do its job. Names the method, never a value."""


class DiscordConfigError(DiscordError):
    """The transport is misconfigured — a missing key, an unusable vault."""


class DiscordSignatureError(DiscordError):
    """A webhook delivery did not carry a valid Ed25519 signature."""


def _resolve_fail_closed(
    value: str | None, key: str, environment: Any | None = None
) -> str:
    """``value``, or the vault's value for ``key`` — never ``""``.

    The one resolution rule this module has, shared by both secrets: an explicit
    value wins, otherwise the vault is asked by name, and a key that resolves to
    ``None`` *or* an empty string raises. ``vault`` never returns ``""`` for a
    missing key, so the empty check is for the explicit argument —
    ``DiscordTransport(bot_token="")`` is a caller asking for no token, and the
    honest answer is an error rather than an unauthenticated request to Discord.
    """
    if value:
        return value
    try:
        env = vault_environment() if environment is None else environment
        resolved = env.resolve(key)
    except SecretStoreError as exc:
        raise DiscordConfigError(
            f"{NAME}: the secret vault is unusable, so {key} cannot be resolved: {exc}"
        ) from exc
    if not resolved:
        raise DiscordConfigError(
            f"{NAME}: no value for {key} from {env.describe()} — add it to the vault, "
            "or pass an explicit value. This transport never falls back to an empty "
            "token or key."
        )
    return resolved


def _header(headers: Any, name: str) -> str | None:
    """One request header, by any casing, or ``None``.

    A web framework hands its headers over in whatever case it likes — an
    ``http.server`` mapping, a WSGI environ, a case-insensitive multidict — so
    the lookup is by *name*, not by key. Discord's own header names are
    title-cased; a caller that passes ``x-signature-ed25519`` is asking the same
    question.
    """
    getter = getattr(headers, "get", None)
    if callable(getter):
        found = getter(name)
        if found is not None:
            return str(found)
    target = str(name).lower()
    items = getattr(headers, "items", None)
    if not callable(items):
        return None
    for key, value in items():
        if str(key).lower() == target:
            return str(value)
    return None


def _external_id(identifier: object) -> str | None:
    """The dedupe key for one inbound event: ``discord:{id}``.

    Discord's snowflakes — message ids and interaction ids — are unique per
    object and stable across redeliveries, which is exactly what the store's
    ``external_id`` index needs. An event that somehow arrived without one gets
    ``None``: no key is better than a shared one, because a single fallback key
    would swallow every such message into one row.
    """
    text = str(identifier or "").strip()
    return f"{NAME}:{text}" if text else None


def _sub_thread(payload: Mapping[str, Any], channel: str) -> str | None:
    """The sub-thread id of ``payload``, or ``None`` when there is no sub-thread.

    Discord nests conversations in channels rather than in a separate thread
    namespace: a message inside a thread carries that thread's *channel* id as
    ``channel_id``, so for the common case there is nothing to add and the
    channel id alone names the conversation. A payload that names a thread
    explicitly (``thread_id``, or the nested ``thread`` object a forum post
    carries) gets it as the key's third component — unless it is the channel
    itself, which :func:`thread_key` documents as the shape that would open a
    fresh thread per message.
    """
    candidate = payload.get("thread_id")
    if candidate is None:
        nested = payload.get("thread")
        if isinstance(nested, Mapping):
            candidate = nested.get("id")
    text = "" if candidate is None else str(candidate).strip()
    if not text or text == channel:
        return None
    return text


def _origin_ref(channel: str, thread_ref: str | None = None) -> str:
    """The thread's addressing, as it is stored on the thread record.

    ``discord:{channel}`` for a top-level conversation and
    ``discord:{channel}:{thread_ref}`` for a nested one. It is the record
    :meth:`DiscordTransport.send` reads back to find out *where* to post: the
    thread id is an opaque key, so the address that survives on the row is what
    keeps outbound from guessing a channel out of the key's shape.
    """
    base = f"{NAME}:{channel}"
    return f"{base}:{thread_ref}" if thread_ref else base


def _interaction_text(payload: Mapping[str, Any]) -> str:
    """The text a signed interaction contributes to the conversation.

    An application command carries its arguments as ``data.options`` (a name and
    a value each, possibly nested), and Discord itself renders the rendered
    invocation as "the command's options"; this does the same, joining the
    string values in order. A message component (a button, a select) has no
    options and is identified by its ``custom_id``, and a command with no
    arguments yet is its own name — so the fallbacks are the command name, and
    an empty string when there is nothing at all to deliver.
    """
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return ""
    parts: list[str] = []

    def collect(options: object) -> None:
        if not isinstance(options, (list, tuple)):
            return
        for option in options:
            if not isinstance(option, Mapping):
                continue
            value = option.get("value")
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
            elif value is not None and not isinstance(value, (Mapping, list, tuple)):
                parts.append(str(value))
            else:
                collect(option.get("options"))

    collect(data.get("options"))
    if parts:
        return " ".join(parts)
    for key in ("custom_id", "name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _message_with_event(
    path: str | None, thread_id: str, event_id: str
) -> ThreadMessage | None:
    """The stored assistant turn for ``event_id``, or ``None`` — the retry probe.

    ``store.append_message`` folds a duplicate ``event_id`` to the stored row,
    which makes the *transcript* idempotent. The post is not:
    ``POST /channels/{id}/messages`` has no client-side dedupe, so a retried
    :meth:`DiscordTransport.send` would append nothing and post a second
    message. This is the read that lets the caller skip the post when the turn
    is already recorded.
    """
    for message in store.messages(thread_id, db=path):
        if message.event_id == event_id:
            return message
    return None


@dataclass
class _GatewayState:
    """What one connection has learned, across the frames of one tick.

    Not a public type: it exists so the heartbeat bookkeeping (the interval the
    gateway asked for, whether the beat it was sent was acknowledged, the last
    sequence number seen, and whether a message event has ended the tick)
    travels with the connection instead of through a handful of parallel locals.
    There is deliberately no clock in here — see :meth:`DiscordTransport.poll_once`.
    """

    interval: float | None = None
    awaiting_ack: bool = False
    last_seq: int | None = None
    saw_message: bool = False


class DiscordTransport:
    """One Discord application, as a :class:`~mini_ork.integrations._base.Transport`.

    Construct it once per process (the bot token is resolved and cached here)
    and pass ``home``/``db`` per call: the transport is long-lived, the target is
    per-request. Nothing in ``__init__`` touches the network; the first outbound
    request is the first :meth:`poll_once` (or :meth:`send`).
    """

    name = NAME

    def __init__(
        self,
        bot_token: str | None = None,
        *,
        public_key: str | None = None,
        api_base: str = DEFAULT_API_BASE,
        recipe: str | None = None,
        home: Any | None = None,
        db: str | None = None,
        origin: ThreadOrigin | str = ThreadOrigin.CHAT,
        ws_timeout: float = DEFAULT_WS_TIMEOUT,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT,
        environment: Any | None = None,
    ) -> None:
        # The bot token is resolved eagerly: it is the one secret *both* tiers
        # need (IDENTIFY carries it, every REST call is authenticated with it),
        # and a transport that cannot authenticate should fail at construction
        # rather than on the first tick of a loop that will retry forever. The
        # public key is per-tier and resolved lazily — see :meth:`public_key`.
        self._bot_token = _resolve_fail_closed(
            bot_token, BOT_TOKEN_VAULT_KEY, environment
        )
        self._public_key = public_key
        self._environment = environment
        self._api_base = api_base.rstrip("/")
        self._recipe = recipe
        self._home = home
        self._db = db
        self._origin = origin
        self._ws_timeout = float(ws_timeout)
        self._http_timeout = float(http_timeout)

    # -- introspection -----------------------------------------------------
    # The token is readable as a property but never rendered: ``__repr__`` is
    # what reaches a log line and a traceback, and it says what the transport is
    # without saying what it holds.
    @property
    def bot_token(self) -> str:
        return self._bot_token

    def public_key(self) -> str:
        """The application's Ed25519 public key, resolved lazily and fail-closed.

        Lazy because a Gateway-only deployment never verifies a signature, and
        asking the vault for a key that was never configured would turn a
        working simple tier into a startup failure. Fail-closed when it *is*
        asked for, for the reason in the module docstring: a webhook with no
        public key cannot verify anything, and accepting anyway is an open
        endpoint.
        """
        return _resolve_fail_closed(
            self._public_key, PUBLIC_KEY_VAULT_KEY, self._environment
        )

    def __repr__(self) -> str:
        return f"<DiscordTransport name={self.name!r} bot_token=***>"

    # -- tiers -------------------------------------------------------------
    def poll_once(
        self,
        *,
        offset: int | None = None,
        timeout: float | None = None,
        home: Any | None = None,
        db: str | None = None,
        sock: Any | None = None,
        limit: int = DEFAULT_FRAME_BUDGET,
    ) -> PollResult:
        """One Gateway tick: connect, drain frames, deliver a message.

        Unlike Telegram's ``getUpdates`` there is no cursor to thread: the
        Gateway is a push stream and a redelivery is deduped by ``external_id``,
        not by an offset, so ``offset`` is accepted (this transport still
        satisfies the :class:`~mini_ork.integrations._base.Transport` protocol)
        and ignored. The tick is *bounded*: one connection, at most ``limit``
        frames, ending at the first message event or when the peer closes. A
        caller that wants to keep listening calls it again — which is also what
        makes the missed-heartbeat report below a value the caller sees rather
        than a reconnect this module hides.

        ``sock`` is forwarded to :func:`mini_ork.integrations._ws.connect` and is
        the whole testability seam — with it, no TCP connection is opened and no
        TLS is negotiated, and the handshake and framing run over whatever socket
        is handed in.

        Nothing here reads a clock. One connection carries one heartbeat, sent as
        soon as HELLO names the cadence, so there is no interval to race and no
        timer to sleep out: the tick is paced by the frames Discord sends, and a
        caller that wants a long-lived session polls again. The one thing that
        must not be missed is a heartbeat nobody answered — that is checked when
        the *peer* closes, which is the only moment it means anything.
        """
        connection = self._open_connection(sock=sock, timeout=timeout)
        try:
            state = _GatewayState()
            results: list[PollResult] = []
            closed_by_peer = False
            for _ in range(max(1, int(limit))):
                try:
                    raw = connection.recv_message()
                except _ws.WebSocketClosed:
                    closed_by_peer = True
                    break
                result = self._handle_frame(
                    raw, connection=connection, state=state, home=home, db=db
                )
                if result is None:
                    continue
                results.append(result)
                if state.saw_message:
                    # The tick has what it came for. Returning here is what keeps
                    # a stream of ignored dispatches from turning one tick into
                    # an unbounded read.
                    return merge_poll_results(results)
            if closed_by_peer:
                self._settle_heartbeat(state)
            return merge_poll_results(results)
        finally:
            # A polite close, but a peer that already vanished must not turn a
            # successful tick into an OSError on the way out.
            try:
                connection.close()
            except OSError:
                pass

    def handle_webhook(
        self,
        headers: Any,
        body: bytes,
        *,
        home: Any | None = None,
        db: str | None = None,
    ) -> Delivery | dict[str, Any] | None:
        """Verify a signed interaction and deliver it. The PONG for a PING.

        ``headers`` is whatever the inbound server has — a mapping readable by
        header name — and ``body`` is the raw request body, because the
        signature is over the bytes Discord sent, not over a re-serialisation of
        them. :meth:`verified_payload` does the check and the parse, in that
        order, so unauthenticated input is never handed to anything that indexes
        it.

        Discord answers an endpoint's registration handshake with a ``type: 1``
        PING and requires a ``type: 1`` PONG back; that is a control message, not
        a turn, so it returns the PONG body (a mapping) and delivers nothing. A
        command or component interaction returns the
        :class:`~mini_ork.threads.service.Delivery`, or ``None`` when it carried
        no text to deliver.
        """
        payload = self.verified_payload(headers, body)
        if _int_or_none(payload.get("type")) == INTERACTION_PING:
            return {"type": PONG_TYPE}
        return self._ingest_interaction(payload, home=home, db=db).delivery

    def verified_payload(self, headers: Any, body: bytes) -> Mapping[str, Any]:
        """Verify then parse. The one gate every webhook path goes through.

        Discord's scheme is Ed25519 over ``X-Signature-Timestamp + raw body``,
        checked with the application's public key. A missing header, a
        malformed hex signature, a bad key and a failed verification all refuse
        with one message, so a failed check tells an attacker nothing about
        which part failed.

        The timestamp is signed but not windowed here: the signature proves the
        body came from Discord, and a *replay* of it is a ``duplicate`` through
        the store's ``external_id`` dedupe rather than a second run — which is a
        property of the thread seam, not a second clock to keep.
        """
        signature = _header(headers, SIGNATURE_HEADER)
        timestamp = _header(headers, TIMESTAMP_HEADER)
        if not signature or not timestamp:
            raise DiscordSignatureError(
                f"{NAME}: the webhook carried no {SIGNATURE_HEADER} / "
                f"{TIMESTAMP_HEADER} pair — refusing the request rather than "
                "accepting it unverified"
            )
        self.verify_ed25519(self.public_key(), timestamp, body, signature)
        return webhooks.parse_payload(body)

    def verify_ed25519(
        self, key_hex: str, timestamp: str, body: bytes, signature_hex: str
    ) -> None:
        """Confirm ``signature_hex`` is Ed25519 over ``timestamp + body``.

        Public because a caller with its own routing (a web framework that wants
        to answer the PING itself) needs the same proof the delivery path needs,
        and duplicating the check is how the two would drift apart. Raises
        :class:`DiscordSignatureError` for anything that is not a valid
        signature, :class:`DiscordConfigError` for a key that is not a usable
        Ed25519 public key, and names the missing extra when ``cryptography``
        itself is not installed.
        """
        public_key_class, invalid_signature = _ed25519()
        try:
            key_bytes = bytes.fromhex(str(key_hex).strip())
            signature_bytes = bytes.fromhex(str(signature_hex).strip())
        except ValueError as exc:
            raise DiscordSignatureError(
                f"{NAME}: the signature or the {PUBLIC_KEY_VAULT_KEY} is not hex — "
                "refusing the request"
            ) from exc
        if len(key_bytes) != 32:
            raise DiscordConfigError(
                f"{NAME}: {PUBLIC_KEY_VAULT_KEY} is {len(key_bytes)} bytes, not the "
                "32 an Ed25519 public key is — the webhook has nothing to verify with"
            )
        message = str(timestamp).encode("utf-8") + bytes(body)
        try:
            key = public_key_class.from_public_bytes(key_bytes)
        except ValueError as exc:
            raise DiscordConfigError(
                f"{NAME}: {PUBLIC_KEY_VAULT_KEY} is not a point on the Ed25519 "
                "curve — the webhook has nothing to verify with"
            ) from exc
        try:
            key.verify(signature_bytes, message)
        except (invalid_signature, ValueError) as exc:
            raise DiscordSignatureError(
                f"{NAME}: the webhook did not carry a valid {SIGNATURE_HEADER} "
                "signature over its timestamp and body — refusing the request"
            ) from exc

    def send(
        self,
        thread_id: str,
        text: str,
        *,
        turn: int | None = None,
        event_id: str | None = None,
        home: Any | None = None,
        db: str | None = None,
    ) -> ThreadMessage:
        """Project an assistant turn onto a thread — and post it to Discord.

        Two writes, in the only order that cannot diverge: the transcript first
        (:func:`mini_ork.threads.store.append_message`, the transport-agnostic
        seam a canvas and Discord both read), then ``POST
        /channels/{channel_id}/messages`` into the channel the thread's
        ``origin_ref`` names. The transcript is the source of truth, so a post
        that fails leaves the turn recorded and a retry with the same
        ``event_id`` re-reads it here and posts once — never a second copy on
        the wire.

        An unmappable thread id — no record, or a record that is not Discord's —
        raises rather than guessing a channel: posting a private conversation
        into the wrong place is worse than not posting. The body carries the
        content only: in Discord a thread *is* a channel, so the destination in
        the URL is the whole addressing and there is no thread field to add.
        """
        path = self._target(home, db)
        channel, thread_ref = self._destination(thread_id, db=path)

        if event_id is not None:
            existing = _message_with_event(path, thread_id, event_id)
            if existing is not None:
                # The turn is already on the transcript — a retry must not post
                # it a second time.
                return existing

        message = store.append_message(
            thread_id,
            role=MessageRole.ASSISTANT,
            text=text,
            turn=turn,
            event_id=event_id,
            db=path,
        )
        destination = thread_ref or channel
        self._api(
            f"channels/{destination}/messages",
            method="POST",
            payload={"content": text},
            token=self._bot_token,
        )
        return message

    # -- gateway internals -------------------------------------------------
    def _open_connection(self, *, sock: Any | None, timeout: float | None):
        """Mint a gateway URL with the bot token, then complete the handshake.

        The URL is needed even when ``sock`` is injected: it names the host for
        the handshake's ``Host`` header and is the only thing ``_ws.connect``
        parses, so a Discord transport never grows a second WebSocket client.
        """
        url = self._gateway_url()
        return _ws.connect(
            url,
            sock=sock,
            timeout=self._ws_timeout if timeout is None else float(timeout),
        )

    def _gateway_url(self) -> str:
        """The Gateway WebSocket URL, per Discord's ``GET /gateway/bot``."""
        payload = self._api("gateway/bot", method="GET", token=self._bot_token)
        url = str(payload.get("url") or "")
        if not url:
            raise DiscordError(
                f"{NAME}: GET /gateway/bot returned no URL to connect to"
            )
        return url

    def _handle_frame(
        self,
        raw: str | bytes,
        *,
        connection: Any,
        state: _GatewayState,
        home: Any | None,
        db: str | None,
    ) -> PollResult | None:
        """Act on one gateway frame: a handshake frame returns ``None``.

        ``None`` means "no outcome to report" — HELLO, an acknowledged heartbeat,
        a dispatch this transport ignores — and is distinct from an ``ignored``
        :class:`PollResult`, which is a *message event* that carried nothing to
        deliver. The distinction matters: a message event ends the tick (that is
        what :attr:`_GatewayState.saw_message` records) while a heartbeat answer
        does not.
        """
        envelope = _decode_frame(raw)
        op = _int_or_none(envelope.get("op"))
        if op == OP_HELLO:
            self._on_hello(envelope, connection, state)
            return None
        if op == OP_HEARTBEAT_ACK:
            state.awaiting_ack = False
            return None
        if op == OP_HEARTBEAT:
            # The gateway asked for an immediate beat. Answering clears nothing:
            # an op 11 for it is still owed, exactly as for a scheduled beat.
            self._send_heartbeat(connection, state)
            return None
        if op != OP_DISPATCH:
            # A reconnect (op 7) or an invalid session (op 9) is Discord telling
            # us to start over — a new *connection*, which is the caller's next
            # tick and not something this one can do without spinning.
            return None

        sequence = envelope.get("s")
        if sequence is not None:
            state.last_seq = _int_or_none(sequence)
        event_type = str(envelope.get("t") or "")
        data = envelope.get("d")
        if event_type != MESSAGE_CREATE_TYPE or not isinstance(data, Mapping):
            return PollResult(action="ignored")
        # Any message event ends the tick — delivered, or ignored because a bot
        # authored it. The tick's question is "is there a message to act on?",
        # and the answer is yes even when the message is not ours to deliver.
        state.saw_message = True
        return self._ingest_message(data, home=home, db=db)

    def _on_hello(
        self, envelope: Mapping[str, Any], connection: Any, state: _GatewayState
    ) -> None:
        """Start the session: IDENTIFY, then the heartbeat HELLO asks for.

        Discord's documented jitter exists to spread a fleet's reconnects across
        an interval, and one laptop's one bot has no fleet to spread: the beat
        goes out at once. The interval is recorded rather than slept out — this
        connection is one tick long, so the first beat is the only one due, and
        the value belongs in the diagnostic if it is never answered.
        """
        data = envelope.get("d")
        interval_ms = data.get("heartbeat_interval") if isinstance(data, Mapping) else None
        state.interval = _heartbeat_seconds(interval_ms)
        self._identify(connection)
        self._send_heartbeat(connection, state)

    def _identify(self, connection: Any) -> None:
        """Send op 2 IDENTIFY with the token and the three intents.

        The one frame that carries the bot token. It goes on the wire and
        nowhere else: never into ``__repr__``, a log line, or an error message.
        """
        connection.send_text(
            json.dumps(
                {
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": self._bot_token,
                        "intents": INTENTS,
                        "properties": {
                            "os": "mini-ork",
                            "browser": "mini-ork",
                            "device": "mini-ork",
                        },
                    },
                }
            )
        )

    def _send_heartbeat(self, connection: Any, state: _GatewayState) -> None:
        """Send op 1 carrying the last sequence number the gateway sent.

        ``None`` before the first sequenced dispatch — Discord's own answer for
        "I have seen nothing yet", and the value the gateway expects to read
        back, not a hole to fill in with a guess.
        """
        connection.send_text(json.dumps({"op": OP_HEARTBEAT, "d": state.last_seq}))
        state.awaiting_ack = True

    def _settle_heartbeat(self, state: _GatewayState) -> None:
        """Report a heartbeat the gateway never acknowledged before it closed.

        Reached when the *peer* closes the connection without a message having
        ended the tick: a gateway that hung up on a beat it never answered (op 11
        is the whole answer) never confirmed it read us, so the socket is dead —
        and a transport that treated that as an ordinary end-of-tick would
        silently re-poll into the same wall. The error is the report; the socket
        is closed by :meth:`poll_once`'s ``finally`` on the way out.
        """
        if not state.awaiting_ack:
            return
        cadence = f"{state.interval:g}s" if state.interval else "its interval"
        raise DiscordError(
            f"{NAME}: the gateway closed with the last heartbeat (op 1) "
            f"unacknowledged — no op 11 arrived within the {cadence} HELLO named, "
            "so the connection is dead"
        )

    def _ingest_message(
        self, message: Mapping[str, Any], *, home: Any | None, db: str | None
    ) -> PollResult:
        """Deliver one MESSAGE_CREATE, or report it ignored.

        Ignored — but not an error — is a bot's own post (including *this* app's
        own ``POST /channels/{id}/messages`` echoed back into a channel the bot
        can read, which without the filter would make the transport answer
        itself), and a message whose content is empty (which is what a bot
        without the privileged MESSAGE_CONTENT intent actually receives).
        """
        author = message.get("author")
        if isinstance(author, Mapping) and author.get("bot"):
            return PollResult(action="ignored")
        text = str(message.get("content") or "")
        if not text.strip():
            return PollResult(action="ignored")
        channel = str(message.get("channel_id") or "")
        if not channel:
            return PollResult(action="ignored")
        return self._deliver(message, channel=channel, text=text, home=home, db=db)

    def _ingest_interaction(
        self, payload: Mapping[str, Any], *, home: Any | None, db: str | None
    ) -> PollResult:
        """Deliver one verified interaction's text, or report it ignored.

        The same seam the Gateway uses, so a command typed into Discord and a
        message typed into it become the same kind of turn on the same kind of
        thread. A bot's own interaction is ignored for the same reason a bot's
        message is.
        """
        user = payload.get("user")
        if not isinstance(user, Mapping):
            member = payload.get("member")
            user = member.get("user") if isinstance(member, Mapping) else None
        if isinstance(user, Mapping) and user.get("bot"):
            return PollResult(action="ignored")
        text = _interaction_text(payload)
        if not text.strip():
            return PollResult(action="ignored")
        channel = str(payload.get("channel_id") or "")
        if not channel:
            return PollResult(action="ignored")
        return self._deliver(payload, channel=channel, text=text, home=home, db=db)

    def _deliver(
        self,
        payload: Mapping[str, Any],
        *,
        channel: str,
        text: str,
        home: Any | None,
        db: str | None,
    ) -> PollResult:
        """The one delivery this transport makes, for both tiers.

        Both a gateway dispatch and a signed interaction carry the same two
        fields — a channel to key on and an id to dedupe by — so both reach the
        state machine through this method, and neither can drift into its own
        idea of what "the same conversation" means.
        """
        thread_ref = _sub_thread(payload, channel)
        thread_id = thread_key(self.name, channel, thread_ref)
        delivery = service.deliver(
            self._home if home is None else home,
            thread_id,
            text,
            title="",
            origin=self._origin,
            origin_ref=_origin_ref(channel, thread_ref),
            recipe=self._recipe,
            # Discord redelivers an unacknowledged gateway event and retries a
            # slow interaction, so the same external_id dedupe the intake tier
            # relies on is what makes a redelivery a no-op here.
            external_id=_external_id(payload.get("id")),
            source=self.name,
            db=self._target(home, db),
        )
        return PollResult(action=delivery.action, delivery=delivery)

    # -- outbound internals ------------------------------------------------
    def _destination(self, thread_id: str, *, db: str | None) -> tuple[str, str | None]:
        """``(channel, thread_ref)`` for a thread id, from its stored record.

        The record is the authority, not the id's shape: the id is an opaque
        slug, and a channel id that happened to contain a ``-`` would be parsed
        wrong. An absent record, or one whose ``origin_ref`` is not this
        transport's, is an error rather than a guess.
        """
        thread = store.get_thread(thread_id, db=db)
        if thread is None:
            raise DiscordError(
                f"{NAME}: no thread {thread_id!r} to post to — open the thread first"
            )
        reference = thread.origin_ref or ""
        parts = reference.split(":", 2)
        if len(parts) < 2 or parts[0] != NAME or not parts[1]:
            raise DiscordError(
                f"{NAME}: thread {thread_id!r} carries no Discord channel to post to "
                f"(origin_ref {reference!r})"
            )
        thread_ref = parts[2] if len(parts) > 2 and parts[2] else None
        return parts[1], thread_ref

    def _target(self, home: Any | None, db: str | None) -> str | None:
        """The db path for this call: the argument, else the instance's, else home.

        Resolved to a *path* here rather than handing ``home`` around, because
        the store and the traces read (``service.deliver``) each take the
        resolved path and must agree on it — the invariant ``deliver``
        documents. The rule is the one it documents, ``db`` → ``home/state.db``,
        spelled here rather than reached for inside ``service`` so this module
        depends on that module's *contract* and not on one of its helpers.
        """
        if db is not None:
            return db
        home = self._home if home is None else home
        if home is None:
            return self._db
        return str(Path(home) / "state.db")

    def _api(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
        token: str,
    ) -> Mapping[str, Any]:
        """One REST call, parsed. Errors never echo the URL or the token.

        The credential rides an ``Authorization: Bot …`` header, so nothing
        secret is in the URL — but the failure message still names only the
        method and the path, because a habit that only holds when the URL
        happens to be safe is not a habit. The exception is chained for a
        traceback in a debugger; the *message* is what a log line should hold.
        """
        url = f"{self._api_base}/{path.lstrip('/')}"
        data = None if payload is None else json.dumps(dict(payload)).encode("utf-8")
        headers = {"Authorization": f"Bot {token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._http_timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise DiscordError(
                f"{NAME}: {method} /{path.lstrip('/')} failed with HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise DiscordError(
                f"{NAME}: {method} /{path.lstrip('/')} could not reach the Discord "
                f"API: {exc.reason}"
            ) from exc

        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            raise DiscordError(
                f"{NAME}: {method} /{path.lstrip('/')} returned a body that is not JSON"
            ) from exc
        if not isinstance(body, Mapping):
            raise DiscordError(
                f"{NAME}: {method} /{path.lstrip('/')} returned JSON that is not an object"
            )
        return body


def _ed25519() -> tuple[Any, type[BaseException]]:
    """``(Ed25519PublicKey, InvalidSignature)`` from a lazily imported ``cryptography``.

    The import lives *inside* the function so that ``import
    mini_ork.integrations.discord`` — the check a deployment with no secrets and
    no extras runs — never touches it. Ed25519 is not hand-rolled here: a
    signature scheme is worth using a library for, which is why this is the one
    place the package gains a dependency, and why a machine without it gets a
    refusal naming the extra rather than an unverified accept.
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise DiscordConfigError(
            f"{NAME}: verifying an interaction needs the integrations extra — "
            f"{EXTRA_INSTALL} — refusing the request rather than accepting it "
            "unverified"
        ) from exc
    return Ed25519PublicKey, InvalidSignature


def _decode_frame(raw: str | bytes) -> Mapping[str, Any]:
    """One gateway frame as a mapping, or a refusal. Remote bytes are never indexed hopefully."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DiscordError(f"{NAME}: the gateway frame was not valid UTF-8") from exc
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DiscordError(f"{NAME}: the gateway frame was not JSON") from exc
    if not isinstance(envelope, Mapping):
        raise DiscordError(f"{NAME}: the gateway frame was not a JSON object")
    return envelope


def _int_or_none(value: Any) -> int | None:
    """``value`` as an int, or ``None`` — Discord sends ids as strings and has been known to send numbers."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _heartbeat_seconds(value: Any) -> float:
    """HELLO's ``heartbeat_interval`` as seconds, floored by :data:`MIN_HEARTBEAT_SECONDS`.

    The wire value is milliseconds; a HELLO that omits it falls back to Discord's
    documented default rather than to "never heartbeat", because a socket that is
    never kept alive is a socket that dies silently.
    """
    millis = _int_or_none(value)
    seconds = DEFAULT_HEARTBEAT_SECONDS if millis is None else millis / 1000.0
    return max(MIN_HEARTBEAT_SECONDS, float(seconds))


__all__ = [
    "BOT_TOKEN_VAULT_KEY",
    "DEFAULT_API_BASE",
    "DEFAULT_FRAME_BUDGET",
    "DEFAULT_HTTP_TIMEOUT",
    "DEFAULT_WS_TIMEOUT",
    "DiscordConfigError",
    "DiscordError",
    "DiscordSignatureError",
    "DiscordTransport",
    "EXTRA_INSTALL",
    "INTENTS",
    "INTERACTION_APPLICATION_COMMAND",
    "INTERACTION_MESSAGE_COMPONENT",
    "INTERACTION_PING",
    "MESSAGE_CREATE_TYPE",
    "NAME",
    "OP_DISPATCH",
    "OP_HEARTBEAT",
    "OP_HEARTBEAT_ACK",
    "OP_HELLO",
    "OP_IDENTIFY",
    "PONG_TYPE",
    "PUBLIC_KEY_VAULT_KEY",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
]
