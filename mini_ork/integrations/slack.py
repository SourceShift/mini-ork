"""Slack, both tiers: Socket Mode for a laptop and an Events API webhook for a team.

The two tiers are the same decision reached two ways, exactly as Telegram's are,
and — the same as there — the split is the point. The *simple* tier is
:meth:`SlackTransport.poll_once`: :meth:`~SlackTransport._connections_open` with
the app-level token (``xapp-…``) over stdlib ``urllib``, then the ``wss`` URL it
returns is handed to :mod:`mini_ork.integrations._ws`. No public URL, no server,
no inbound port: the thing a person trying mini-ork on a laptop can actually
configure. Slack pushes envelopes down that socket, this transport acks each one
and delivers any user message it carries. The *secure* tier is
:meth:`SlackTransport.handle_webhook`: an inbound port, Slack's own ``v0``
signature over the raw body, and a ``±300 s`` freshness window, which is what a
team with a public URL and a signing secret wants. Neither is a toy of the
other; one class serves both because what differs is transport mechanics while
what matters — which thread a message lands on — is shared.

**The wire format is not re-implemented here.** The RFC 6455 client is
:mod:`mini_ork.integrations._ws`, hand-written and stdlib-only for the reason
that module documents, and this transport only asks it to ``connect``,
``recv_message`` and ``send_text``. The HMAC *shape* — ``hmac.new(...,
sha256).hexdigest()`` compared with :func:`hmac.compare_digest`, parse only
after verify — is the one :mod:`mini_ork.intake.webhooks` established; Slack's
``v0`` dialect (``v0:{timestamp}:{raw body}``, plus the freshness window) is this
module's, because it is Slack's and nobody else's.

**No new dependency, on purpose.** ``pyproject.toml``'s runtime dependencies are
``PyYAML`` and ``jsonschema`` and this module does not move them: socket mode is
``urllib.request`` for the one call that mints a URL and ``_ws`` for the socket;
the signature is ``hmac`` + ``hashlib``. A transport that required
``slack_sdk`` would make "install mini-ork and talk to Slack" a two-step, and the
simple tier exists to be one.

**The three secrets fail closed.** ``SLACK_BOT_TOKEN`` (``xoxb-…``) is resolved
eagerly, the way the Telegram token is: a transport that cannot authenticate
should fail at construction, not on the first tick of a retry loop. The other
two are resolved lazily and by key name — ``SLACK_APP_TOKEN`` (``xapp-…``) by
socket mode, ``SLACK_SIGNING_SECRET`` by the webhook — because a socket-mode-only
deployment never needs a signing secret and a webhook-only deployment never needs
an app token, and asking the vault for a key that was never configured would turn
a working tier into a startup failure. Fail-closed whenever the key *is* asked
for, for the reason :func:`_resolve_fail_closed` spells out: an empty signing
secret would be an endpoint that accepts forged input, which is not a degraded
mode. No key ever reaches ``__repr__``, a log line, or an exception message.

**The thread is the state machine's, not this module's.** Inbound goes through
:func:`mini_ork.threads.service.deliver` and nothing else, so the four outcomes
(``launched``/``steered``/``continued``/``duplicate``) are the ones the canvas
and work-intake already share, and a redelivered Slack event is deduped by
``external_id`` (``slack:{event_id}``) rather than by a cache this module would
have to keep warm. Outbound is :meth:`SlackTransport.send`: it posts the turn
with ``chat.postMessage`` and *also* projects it onto the thread's transcript via
:func:`mini_ork.threads.store.append_message` — the transport-agnostic seam — so
a canvas and Slack read the same conversation and outbound cannot diverge from
what the thread says. The transcript write is what makes a retried send a no-op
instead of a second message on the wire.

**What is deliberately missing.** No ``apps.connections.open`` reconnection
loop, no rate-limit backoff, no interactive components (buttons, modals), no
file uploads, and no ``ws://`` tier (``_ws`` refuses it). Each is a real feature
of the platform and none is a feature of the conversation seam the transports
exist to be; adding one later is an edit here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from mini_ork.dispatch.secrets import SecretStoreError
from mini_ork.dispatch.vault import vault_environment
from mini_ork.intake import webhooks
from mini_ork.threads import service, store
from mini_ork.threads.model import MessageRole, ThreadMessage, ThreadOrigin
from mini_ork.threads.service import Delivery

from . import _ws
from ._base import PollResult, merge_poll_results, thread_key

#: The transport's id, and the first component of every :func:`thread_key` it
#: mints. A constant because it is also the default ``source`` on the steering
#: signal and the origin-ref prefix — one string, one meaning.
NAME = "slack"

#: The vault keys the three secrets are resolved from, named exactly as the
#: deployment writes them. Named (not inlined) because the error message echoes
#: the key: "which key is missing" is the whole diagnostic a person gets, and it
#: must match the name they wrote in their vault. Deliberately *not* Telegram's
#: dotted ``integrations.<name>.<key>`` convention — Slack's tokens are what an
#: operator copies out of the app's config page and exports under these names.
BOT_TOKEN_VAULT_KEY = "SLACK_BOT_TOKEN"
APP_TOKEN_VAULT_KEY = "SLACK_APP_TOKEN"
SIGNING_SECRET_VAULT_KEY = "SLACK_SIGNING_SECRET"

#: Where the Web API lives. Overridable so a test — or a proxy in front of
#: Slack — never has to reach slack.com.
DEFAULT_API_BASE = "https://slack.com/api"

#: The two headers a signed Events API delivery carries. The signature is the
#: positional ``header`` on :meth:`SlackTransport.handle_webhook` (matching the
#: :class:`~mini_ork.integrations._base.Transport` shape); the timestamp is a
#: keyword, because freshness is part of *this* module's check.
SIGNATURE_HEADER = "x-slack-signature"
TIMESTAMP_HEADER = "x-slack-request-timestamp"

#: Slack's signature scheme id. It is a version tag, not a key id: it is the
#: prefix of both the base string and the header value, so a future rotation to
#: ``v1`` is a rejected signature rather than a silent mis-parse.
SIGNATURE_VERSION = "v0"

#: How far a request's timestamp may be from this machine's clock. Slack's own
#: recommendation; without it a captured request replays forever, signature and
#: all, because the signature proves possession of the secret and nothing about
#: *when* the body was sent.
MAX_SKEW_SECONDS = 300

#: Envelope and event vocabulary, as constants because each appears in a
#: predicate a reader has to be able to grep.
EVENTS_ENVELOPE_TYPE = "events_api"
URL_VERIFICATION_TYPE = "url_verification"
EVENT_CALLBACK_TYPE = "event_callback"
MESSAGE_EVENT_TYPE = "message"

#: How long the socket-mode read and the Web API call may block when the caller
#: does not say. Not a nicety: a gateway that goes silent mid-read must fail the
#: caller rather than pin the thread running it.
DEFAULT_WS_TIMEOUT = 30.0
DEFAULT_HTTP_TIMEOUT = 30.0


class SlackError(RuntimeError):
    """A Slack transport could not do its job. Names the method, never a value."""


class SlackConfigError(SlackError):
    """The transport is misconfigured — a missing token or secret. Fail closed."""


class SlackSignatureError(SlackError):
    """A webhook delivery did not carry a valid, fresh signature."""


def _resolve_fail_closed(
    value: str | None, key: str, environment: Any | None = None
) -> str:
    """``value``, or the vault's value for ``key`` — never ``""``.

    The one resolution rule this module has, shared by all three secrets: an
    explicit value wins, otherwise the vault is asked by name, and a key that
    resolves to ``None`` *or* an empty string raises. ``vault`` never returns
    ``""`` for a missing key, so the empty check is for the explicit argument —
    ``SlackTransport(bot_token="")`` is a caller asking for no token, and the
    honest answer is an error rather than an unsigned request to Slack.
    """
    if value:
        return value
    try:
        env = vault_environment() if environment is None else environment
        resolved = env.resolve(key)
    except SecretStoreError as exc:
        raise SlackConfigError(
            f"{NAME}: the secret vault is unusable, so {key} cannot be resolved: {exc}"
        ) from exc
    if not resolved:
        raise SlackConfigError(
            f"{NAME}: no value for {key} from {env.describe()} — add it to the vault, "
            "or pass an explicit value. This transport never falls back to an empty "
            "token or secret."
        )
    return resolved


def sign_v0(secret: str, body: bytes, timestamp: str | int) -> str:
    """The ``X-Slack-Signature`` value ``body`` should carry at ``timestamp``.

    Slack's scheme, not the house one: the signed base is
    ``v0:{timestamp}:{body}``, and the header repeats the version tag
    (``v0=<hex>``). Exposed so a test — or a relay re-signing a proxied body —
    can be honest about what it produced rather than hand-rolling the HMAC.
    """
    base = f"{SIGNATURE_VERSION}:{timestamp}:".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def verify_v0(
    secret: str,
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    *,
    now: float | None = None,
) -> bool:
    """Whether ``signature`` is Slack's signature of exactly ``body``, freshly.

    Constant-time, and against the whole string rather than the digest alone:
    the version prefix is public, but comparing end to end means there is no
    branch on secret-derived content anywhere in here. A missing header, an
    unfamiliar version, a wrong digest and a stale timestamp are all just
    ``False`` — the caller turns that into one refusal with one message, so a
    failed check tells an attacker nothing about which part failed.

    ``now`` is injectable so a test can pin the clock; the default is this
    machine's, which is the whole point of the window.
    """
    if not signature or not timestamp:
        return False
    stamp = str(timestamp).strip()
    if not stamp:
        return False
    try:
        seconds = int(stamp)
    except ValueError:
        return False
    reference = time.time() if now is None else float(now)
    if abs(reference - seconds) > MAX_SKEW_SECONDS:
        return False
    presented = signature.strip().lower()
    if not presented.startswith(f"{SIGNATURE_VERSION}="):
        return False
    return hmac.compare_digest(sign_v0(secret, body, stamp), presented)


def _is_user_message(event: Mapping[str, Any]) -> bool:
    """Whether ``event`` is a person's message this transport should deliver.

    Delivered: a fresh ``message`` with text, no ``subtype``, and no bot
    identity. Ignored — but still acked — is everything else: an edit is a
    ``message_changed`` subtype, a bot's post (including *this* app's own
    ``chat.postMessage`` echoed back into a channel the app is in) carries
    ``bot_id`` or ``bot_profile``, and a join/reaction/other event is simply not
    a ``message``. The bot filter is load-bearing, not defensive: without it a
    transport that posts to a channel it listens to would answer itself.
    """
    if str(event.get("type") or "") != MESSAGE_EVENT_TYPE:
        return False
    if event.get("subtype"):
        return False
    if event.get("bot_id") or event.get("bot_profile"):
        return False
    return True


def _external_id(
    body: Mapping[str, Any], event: Mapping[str, Any], *, channel: object, ts: object
) -> str:
    """The dedupe key for one event: ``slack:{event_id}``.

    Slack's ``event_id`` is unique per event and stable across redeliveries,
    which is exactly what the store's ``external_id`` index needs. The fallbacks
    (a client's own message id, then the channel/ts pair) keep a message that
    somehow arrived without an ``event_id`` from collapsing to a single key that
    would swallow every such message.
    """
    identifier = str(body.get("event_id") or "").strip()
    if not identifier:
        identifier = str(event.get("client_msg_id") or "").strip()
    if not identifier:
        identifier = f"{channel}:{ts}"
    return f"{NAME}:{identifier}"


def _origin_ref(channel: object, thread_ts: object | None) -> str:
    """The thread's addressing, as it is stored on the thread record.

    ``slack:{channel}`` for a top-level conversation and ``slack:{channel}:
    {thread_ts}`` for a threaded one. It is the record :meth:`SlackTransport.send`
    reads back to find out *where* to post: the thread id is an opaque key, and
    this is the address that survives on the row, so outbound never has to guess
    a channel from the key's shape.
    """
    base = f"{NAME}:{channel}"
    return f"{base}:{thread_ts}" if thread_ts else base


def _message_with_event(
    path: str, thread_id: str, event_id: str
) -> ThreadMessage | None:
    """The stored assistant turn for ``event_id``, or ``None`` — the retry probe.

    ``store.append_message`` folds a duplicate ``event_id`` to the stored row,
    which makes the *transcript* idempotent. The post is not: ``chat.postMessage``
    has no client-side dedupe, so a retried :meth:`SlackTransport.send` would
    append nothing and post a second message. This is the read that lets the
    caller skip the post when the turn is already recorded.
    """
    for message in store.messages(thread_id, db=path):
        if message.event_id == event_id:
            return message
    return None


class SlackTransport:
    """One Slack app, as a :class:`~mini_ork.integrations._base.Transport`.

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
        app_token: str | None = None,
        signing_secret: str | None = None,
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
        # need (socket mode posts replies, the webhook answers with them), and a
        # transport that cannot authenticate should fail at construction rather
        # than on the first tick of a loop that will retry forever. The other two
        # are per-tier and resolved lazily — see :meth:`app_token`.
        self._bot_token = _resolve_fail_closed(
            bot_token, BOT_TOKEN_VAULT_KEY, environment
        )
        self._app_token = app_token
        self._signing_secret = signing_secret
        self._environment = environment
        self._api_base = api_base.rstrip("/")
        self._recipe = recipe
        self._home = home
        self._db = db
        self._origin = origin
        self._ws_timeout = float(ws_timeout)
        self._http_timeout = float(http_timeout)

    # -- introspection -----------------------------------------------------
    # The tokens are readable as properties but never rendered: ``__repr__`` is
    # what reaches a log line and a traceback, and it says what the transport is
    # without saying what it holds.
    @property
    def bot_token(self) -> str:
        return self._bot_token

    def app_token(self) -> str:
        """The app-level token, resolved lazily and fail-closed.

        Lazy because a webhook-only deployment never needs it, and asking the
        vault for a key that was never configured would turn a working secure
        tier into a startup failure. Fail-closed when it *is* asked for, for the
        reason in the module docstring.
        """
        return _resolve_fail_closed(
            self._app_token, APP_TOKEN_VAULT_KEY, self._environment
        )

    def signing_secret(self) -> str:
        """The signing secret, resolved lazily and fail-closed.

        The mirror of :meth:`app_token` for the other tier: a socket-mode-only
        deployment never needs it, and an unresolvable secret refuses a webhook
        rather than accepting an unsigned one.
        """
        return _resolve_fail_closed(
            self._signing_secret, SIGNING_SECRET_VAULT_KEY, self._environment
        )

    def __repr__(self) -> str:
        return f"<SlackTransport name={self.name!r} bot_token=***>"

    # -- tiers -------------------------------------------------------------
    def poll_once(
        self,
        *,
        offset: int | None = None,
        timeout: float | None = None,
        home: Any | None = None,
        db: str | None = None,
        sock: Any | None = None,
        limit: int = 1,
    ) -> PollResult:
        """One Socket Mode tick: connect, ack and deliver up to ``limit`` envelopes.

        Unlike Telegram's ``getUpdates`` there is no cursor to thread: Slack's
        socket is a push stream and a redelivery is deduped by ``external_id``,
        not by an offset. ``offset`` is therefore accepted (so this transport
        still satisfies the :class:`~mini_ork.integrations._base.Transport`
        protocol) and ignored. The tick is *bounded* — one connection, ``limit``
        envelopes — because that is what a poll loop and a test both need: a
        caller that wants to keep listening calls it again.

        Every envelope is acked with ``{"envelope_id": …}`` *before* it is
        processed, whether or not it carries anything to deliver: Slack
        redelivers anything it does not see acked, so an ignored message that was
        not acked would come back forever.

        ``sock`` is forwarded to :func:`mini_ork.integrations._ws.connect` and is
        the whole testability seam — with it, no TCP connection is opened and no
        TLS is negotiated, and the handshake and framing run over whatever socket
        is handed in (a ``socketpair`` in the tests, a connection a Discord
        transport built itself, …).
        """
        connection = self._open_connection(sock=sock, timeout=timeout)
        try:
            results: list[PollResult] = []
            for _ in range(max(1, int(limit))):
                try:
                    raw = connection.recv_message()
                except _ws.WebSocketClosed:
                    # The peer hung up between envelopes: the tick is over, not
                    # an error. What was already delivered stays delivered.
                    break
                results.append(self._handle_frame(raw, connection=connection, home=home, db=db))
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
        body: bytes,
        header: str | None,
        *,
        timestamp: str | None = None,
        home: Any | None = None,
        db: str | None = None,
    ) -> Delivery | None:
        """Verify an Events API delivery and deliver it. ``None`` when it carried none.

        ``header`` is the ``X-Slack-Signature`` value and ``timestamp`` the
        ``X-Slack-Request-Timestamp`` one. Both are required and both are
        checked with :func:`verify_v0`: a wrong signature and a stale timestamp
        each raise :class:`SlackSignatureError` — there is no local-caller escape
        hatch on this path, for the reason spelled out in
        :mod:`mini_ork.intake.webhooks`.

        The body is parsed only *after* it verifies: :func:`webhooks.parse_payload`
        turns remote bytes into a mapping or refuses, and doing it in that order
        means unauthenticated input is never handed to anything that indexes it.

        Slack's URL-verification handshake is not a delivery, so it returns
        ``None`` here; answer it with :meth:`webhook_challenge`.
        """
        payload = self.verified_payload(body, header, timestamp=timestamp)
        if str(payload.get("type") or "") == URL_VERIFICATION_TYPE:
            return None
        return self._ingest(payload, home=home, db=db).delivery

    def webhook_challenge(
        self, body: bytes, header: str | None, *, timestamp: str | None = None
    ) -> str | None:
        """The ``challenge`` of a verified ``url_verification`` request, or ``None``.

        Slack refuses to enable an Events API endpoint until it is answered with
        the challenge it sent, and that request carries the same signature as any
        other delivery — so it is verified with the same call, and only then is
        the challenge read back out. An unsigned "handshake" gets the same
        refusal a forged delivery does.
        """
        payload = self.verified_payload(body, header, timestamp=timestamp)
        if str(payload.get("type") or "") != URL_VERIFICATION_TYPE:
            return None
        return str(payload.get("challenge") or "")

    def verified_payload(
        self, body: bytes, header: str | None, *, timestamp: str | None = None
    ) -> Mapping[str, Any]:
        """Verify then parse. The one gate both webhook methods go through.

        Public because the challenge path needs the same proof as the delivery
        path, and duplicating the check is how the two would drift apart.
        """
        secret = self.signing_secret()
        if not verify_v0(secret, body, header, timestamp):
            raise SlackSignatureError(
                f"{NAME}: the webhook did not carry a valid {SIGNATURE_VERSION} "
                "signature, or its timestamp is outside the freshness window — "
                "refusing the request"
            )
        return webhooks.parse_payload(body)

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
        """Project an assistant turn onto a thread — and post it to Slack.

        Two writes, in the only order that cannot diverge: the transcript first
        (:func:`mini_ork.threads.store.append_message`, the transport-agnostic
        seam a canvas and Slack both read), then ``chat.postMessage`` into the
        channel and thread the thread's ``origin_ref`` names. The transcript is
        the source of truth, so a post that fails leaves the turn recorded and a
        retry with the same ``event_id`` re-reads it here and posts once — never
        a second copy on the wire.

        An unmappable thread id — no record, or a record that is not Slack's —
        raises rather than guessing a channel: posting a private conversation
        into the wrong place is worse than not posting.
        """
        path = self._target(home, db)
        channel, thread_ts = self._destination(thread_id, db=path)

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
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts is not None:
            # Only a threaded conversation carries thread_ts; a top-level one
            # posts at the channel root, which is what Slack means by omitting it.
            payload["thread_ts"] = thread_ts
        self._api("chat.postMessage", payload, token=self._bot_token)
        return message

    # -- internals ---------------------------------------------------------
    def _open_connection(self, *, sock: Any | None, timeout: float | None):
        """Mint a socket-mode URL with the app token, then complete the handshake.

        The URL is needed even when ``sock`` is injected: it names the host for
        the handshake's ``Host`` header and is the only thing ``_ws.connect``
        parses. It is never rendered anywhere — Slack's ``wss`` URLs carry a
        ticket in their query string, exactly as the Telegram transport's
        request URLs carry the bot token.
        """
        url = self._connections_open()
        return _ws.connect(
            url,
            sock=sock,
            timeout=self._ws_timeout if timeout is None else float(timeout),
        )

    def _connections_open(self) -> str:
        """The Socket Mode WebSocket URL, per Slack's ``apps.connections.open``."""
        payload = self._api("apps.connections.open", {}, token=self.app_token())
        url = str(payload.get("url") or "")
        if not url:
            raise SlackError(
                f"{NAME}: apps.connections.open returned no URL to connect to"
            )
        return url

    def _handle_frame(
        self,
        raw: str | bytes,
        *,
        connection: Any,
        home: Any | None,
        db: str | None,
    ) -> PollResult:
        """Ack one socket-mode frame, then act on it.

        The ack comes first and unconditionally: Slack redelivers an envelope it
        does not see acked, and an envelope this transport *ignores* is still an
        envelope it received — so the ack is sent before anything is parsed, and
        a ``hello``/``disconnect`` frame (which carries no ``envelope_id``) is
        simply left unacked because there is nothing to ack.
        """
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SlackError(
                    f"{NAME}: the socket-mode frame was not valid UTF-8"
                ) from exc
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SlackError(f"{NAME}: the socket-mode frame was not JSON") from exc
        if not isinstance(envelope, Mapping):
            raise SlackError(f"{NAME}: the socket-mode frame was not a JSON object")

        envelope_id = envelope.get("envelope_id")
        if envelope_id:
            connection.send_text(json.dumps({"envelope_id": envelope_id}))
        return self._ingest_envelope(envelope, home=home, db=db)

    def _ingest_envelope(
        self, envelope: Mapping[str, Any], *, home: Any | None, db: str | None
    ) -> PollResult:
        """Deliver one socket-mode envelope, or report it ignored.

        Socket mode wraps the same Events API body a webhook would POST under
        ``payload``, so unwrapping it here is what lets :meth:`_ingest` serve
        both tiers with one predicate set.
        """
        if str(envelope.get("type") or "") != EVENTS_ENVELOPE_TYPE:
            return PollResult(action="ignored")
        body = envelope.get("payload")
        if not isinstance(body, Mapping):
            return PollResult(action="ignored")
        return self._ingest(body, home=home, db=db)

    def _ingest(
        self, body: Mapping[str, Any], *, home: Any | None, db: str | None
    ) -> PollResult:
        """Deliver one Events API body, or report it ignored with no cursor.

        An event this transport ignores is not an error and not a delivery: it
        still carries an ``event_id`` Slack will redeliver if it is not acked,
        which is why the ack happens in :meth:`_handle_frame` regardless and the
        ``ignored`` result is a deliberate value rather than a dropped envelope.
        """
        if str(body.get("type") or "") != EVENT_CALLBACK_TYPE:
            return PollResult(action="ignored")
        event = body.get("event")
        if not isinstance(event, Mapping) or not _is_user_message(event):
            return PollResult(action="ignored")
        text = str(event.get("text") or "")
        if not text.strip():
            return PollResult(action="ignored")

        channel = event.get("channel") or ""
        ts = event.get("ts") or ""
        # thread_ts is Slack's own distinction: present means "inside a thread",
        # absent means "at the channel root". Passing it straight through keeps a
        # reply in a thread on the thread's key and a root message on the
        # channel's, exactly as the thread_key contract spells out.
        thread_ts = event.get("thread_ts") or None
        thread_id = thread_key(self.name, channel, thread_ts)

        delivery = service.deliver(
            self._home if home is None else home,
            thread_id,
            text,
            title="",
            origin=self._origin,
            origin_ref=_origin_ref(channel, thread_ts),
            recipe=self._recipe,
            # Slack redelivers an envelope until it is acked, so the same
            # external_id dedupe the intake tier relies on is what makes a
            # redelivery a no-op here.
            external_id=_external_id(body, event, channel=channel, ts=ts),
            source=self.name,
            db=self._target(home, db),
        )
        return PollResult(action=delivery.action, delivery=delivery)

    def _destination(self, thread_id: str, *, db: str | None) -> tuple[str, str | None]:
        """``(channel, thread_ts)`` for a thread id, from its stored record.

        The record is the authority, not the id's shape: the id is an opaque
        slug and a channel id that happened to contain a ``-`` would be parsed
        wrong. An absent record, or one whose ``origin_ref`` is not this
        transport's, is an error rather than a guess.
        """
        thread = store.get_thread(thread_id, db=db)
        if thread is None:
            raise SlackError(
                f"{NAME}: no thread {thread_id!r} to post to — open the thread first"
            )
        reference = thread.origin_ref or ""
        parts = reference.split(":", 2)
        if len(parts) < 2 or parts[0] != NAME or not parts[1]:
            raise SlackError(
                f"{NAME}: thread {thread_id!r} carries no Slack channel to post to "
                f"(origin_ref {reference!r})"
            )
        thread_ts = parts[2] if len(parts) > 2 and parts[2] else None
        return parts[1], thread_ts

    def _target(self, home: Any | None, db: str | None) -> str | None:
        """The db path for this call: the argument, else the instance's, else home.

        Resolved to a *path* here rather than handing ``home`` around, because
        the store and the traces read (``service.deliver``) each take the
        resolved path and must agree on it — the invariant ``deliver`` documents.
        The rule is the one it documents, ``db`` → ``home/state.db``, and it is
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
        self, method: str, payload: Mapping[str, Any], *, token: str
    ) -> Mapping[str, Any]:
        """One Web API POST, parsed. Errors never echo the URL or the token.

        The credential rides an ``Authorization: Bearer`` header, so — unlike
        Telegram's token-in-path API — nothing secret is in the URL. The failure
        message still names only the method and the status, because a habit that
        only holds when the URL happens to be safe is not a habit. The exception
        is chained for a traceback in a debugger; the *message* is what a log
        line should hold.
        """
        url = f"{self._api_base}/{method}"
        data = json.dumps(dict(payload)).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._http_timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise SlackError(f"{NAME}: {method} failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise SlackError(
                f"{NAME}: {method} could not reach the Slack API: {exc.reason}"
            ) from exc

        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SlackError(f"{NAME}: {method} returned a body that is not JSON") from exc
        if not isinstance(body, Mapping) or not body.get("ok"):
            reason = ""
            if isinstance(body, Mapping):
                reason = str(body.get("error") or "")
            raise SlackError(
                f"{NAME}: {method} was refused by Slack: {reason or 'no reason given'}"
            )
        return body


__all__ = [
    "APP_TOKEN_VAULT_KEY",
    "BOT_TOKEN_VAULT_KEY",
    "DEFAULT_API_BASE",
    "DEFAULT_HTTP_TIMEOUT",
    "DEFAULT_WS_TIMEOUT",
    "EVENTS_ENVELOPE_TYPE",
    "MAX_SKEW_SECONDS",
    "NAME",
    "SIGNATURE_HEADER",
    "SIGNATURE_VERSION",
    "SIGNING_SECRET_VAULT_KEY",
    "TIMESTAMP_HEADER",
    "SlackConfigError",
    "SlackError",
    "SlackSignatureError",
    "SlackTransport",
    "sign_v0",
    "verify_v0",
]
