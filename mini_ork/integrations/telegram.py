"""Telegram, both tiers: a stdlib long poll and an HMAC-verified webhook.

The two tiers are the same decision reached two ways, exactly as
:mod:`mini_ork.intake` splits polling from webhooks — and the split is the point.
The *simple* tier is :meth:`TelegramTransport.poll_once`: outbound HTTPS only,
no server, no public URL, stdlib ``urllib`` and nothing else, which is what a
person trying mini-ork on a laptop can actually run. The *secure* tier is
:meth:`TelegramTransport.handle_webhook`: an inbound port, a shared secret, and
a signature check, which is what a team with a public URL and a secret store
wants. Neither is a toy of the other; each is complete for its deployment, and
one class serves both because the part that differs is transport mechanics while
the part that matters — which thread a message lands on — is shared.

**No new dependency, on purpose.** ``pyproject.toml``'s runtime dependencies are
``PyYAML`` and ``jsonschema`` and this module does not move them: the long poll
is ``urllib.request`` and the signature is :func:`mini_ork.intake.webhooks.sign`
(``hmac`` + ``hashlib``). A transport that required ``requests`` would make
"install mini-ork and talk to Telegram" a two-step, and the simple tier exists
to be one.

**The token fails closed.** It is resolved from the vault by key name
(:data:`TOKEN_VAULT_KEY`), and a key that resolves to nothing raises
:class:`TelegramConfigError` naming the key. It is never defaulted to ``""``:
``vault_environment().resolve`` returns ``None`` — not ``""`` — for a missing
key precisely so that this cannot happen by accident, and the whole reason is
security-bearing. An empty token on the long-poll tier is a request Telegram
rejects, which is merely useless; the same default on a webhook tier would be an
endpoint that accepts unsigned input, which is not. One rule for both: a missing
token is an error, never a fallback.

**The thread is the state machine's, not this module's.** Inbound goes through
:func:`mini_ork.threads.service.deliver` and nothing else, so the four outcomes
(``launched``/``steered``/``continued``/``duplicate``) are the ones the canvas
and work-intake already share, and a redelivered Telegram update is deduped by
``external_id`` rather than by a cache this module would have to keep warm.
Outbound is :meth:`TelegramTransport.send`, a thin projection of the assistant's
words onto the thread's transcript via
:func:`mini_ork.threads.store.append_message` — the transport-agnostic seam, so
a canvas and Telegram read the same conversation. It does not POST to the
Telegram API yet; a real outbound send is the next stage, and it is queued
behind the same transcript so it cannot diverge from what the thread says.
"""

from __future__ import annotations

import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from mini_ork.dispatch.secrets import SecretStoreError
from mini_ork.dispatch.vault import vault_environment
from mini_ork.intake import webhooks
from mini_ork.threads import service, store
from mini_ork.threads.model import MessageRole, ThreadMessage, ThreadOrigin
from mini_ork.threads.service import Delivery

from ._base import PollResult, merge_poll_results, thread_key

#: The transport's id, and the first component of every :func:`thread_key` it
#: mints. A constant because it is also the default ``source`` on the steering
#: signal and the origin-ref prefix — one string, one meaning.
NAME = "telegram"

#: The vault key the bot token is resolved from. Named (not inlined) because the
#: error message echoes it: "which key is missing" is the whole diagnostic a
#: person gets, and it must match the name they wrote in their vault.
TOKEN_VAULT_KEY = "integrations.telegram.bot_token"

#: The vault key a webhook's shared secret is resolved from. The same
#: fail-closed rule as the token: an unresolvable secret refuses the request
#: rather than accepting an unsigned one.
WEBHOOK_SECRET_VAULT_KEY = "integrations.telegram.webhook_secret"

#: Where the Bot API lives. Overridable so a test — or a self-hosted Bot API
#: server — never has to reach api.telegram.org.
DEFAULT_API_BASE = "https://api.telegram.org"

#: The signature header a relay in front of this endpoint is expected to set.
#: Deliberately *not* Telegram's own ``X-Telegram-Bot-Api-Secret-Token``: this
#: reuses :func:`~mini_ork.intake.webhooks.verify` verbatim, and that function
#: speaks one dialect (``sha256=<hex>`` over the raw body). See
#: :meth:`TelegramTransport.handle_webhook` for the native header, which the
#: secure tier also accepts when a caller configures it.
SIGNATURE_HEADER = "x-telegram-signature-256"

#: Telegram's own secret-token header. Verified by constant-time comparison
#: when a deployment sets it up, which is the only way the Bot API's native
#: webhook auth can be honored without inventing a second HMAC scheme.
SECRET_TOKEN_HEADER = "x-telegram-bot-api-secret-token"

#: How long a long poll holds the connection open when the caller does not say.
#: Telegram's own default and ceiling reasoning: the poll *is* the idle time, so
#: a short one is a busy loop against the API and a long one is a stalled
#: process on shutdown.
DEFAULT_LONG_POLL_SECONDS = 30

#: The HTTP read timeout. Greater than the long-poll window, or every poll would
#: time out client-side at the exact moment the server was about to answer.
DEFAULT_HTTP_TIMEOUT = DEFAULT_LONG_POLL_SECONDS + 10


class TelegramError(RuntimeError):
    """A Telegram transport could not do its job. Names the key, never the value."""


class TelegramConfigError(TelegramError):
    """The transport is misconfigured — a missing token or secret. Fail closed."""


class TelegramSignatureError(TelegramError):
    """A webhook body did not carry this endpoint's signature."""


def _resolve_fail_closed(
    value: str | None, key: str, environment: Any | None = None
) -> str:
    """``value``, or the vault's value for ``key`` — never ``""``.

    The one resolution rule this module has, shared by the token and the webhook
    secret: an explicit value wins, otherwise the vault is asked by name, and a
    key that resolves to ``None`` *or* an empty string raises. ``vault`` never
    returns ``""`` for a missing key, so the empty check is for the explicit
    argument — ``TelegramTransport(token="")`` is a caller asking for no token,
    and the honest answer is an error rather than a request to ``/bot/getUpdates``.
    """
    if value:
        return value
    try:
        env = vault_environment() if environment is None else environment
        resolved = env.resolve(key)
    except SecretStoreError as exc:
        raise TelegramConfigError(
            f"{NAME}: the secret vault is unusable, so {key} cannot be resolved: {exc}"
        ) from exc
    if not resolved:
        raise TelegramConfigError(
            f"{NAME}: no value for {key} from {env.describe()} — add it to the vault, "
            "or pass an explicit value. This transport never falls back to an empty "
            "token or secret."
        )
    return resolved


class TelegramTransport:
    """One Telegram bot, as a :class:`~mini_ork.integrations._base.Transport`.

    Construct it once per process (the token is resolved and cached here) and
    pass ``home``/``db`` per call: the transport is long-lived, the target is
    per-request. Nothing in ``__init__`` touches the network; the first outbound
    request is the first :meth:`poll_once`.
    """

    name = NAME

    def __init__(
        self,
        token: str | None = None,
        *,
        webhook_secret: str | None = None,
        secret_token: str | None = None,
        api_base: str = DEFAULT_API_BASE,
        recipe: str | None = None,
        home: Any | None = None,
        db: str | None = None,
        origin: ThreadOrigin | str = ThreadOrigin.CHAT,
        long_poll_seconds: int = DEFAULT_LONG_POLL_SECONDS,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT,
        environment: Any | None = None,
    ) -> None:
        # Resolved eagerly: a transport that cannot authenticate should fail at
        # construction, not on the first tick of a loop that will retry forever.
        self._token = _resolve_fail_closed(token, TOKEN_VAULT_KEY, environment)
        self._environment = environment
        self._webhook_secret = webhook_secret
        self._secret_token = secret_token
        self._api_base = api_base.rstrip("/")
        self._recipe = recipe
        self._home = home
        self._db = db
        self._origin = origin
        self._long_poll_seconds = int(long_poll_seconds)
        self._http_timeout = float(http_timeout)

    # -- introspection -----------------------------------------------------
    # The token is readable as a property but never rendered: ``__repr__`` is
    # what reaches a log line and a traceback, and it says what the transport is
    # without saying what it holds.
    @property
    def token(self) -> str:
        return self._token

    def webhook_secret(self) -> str:
        """The webhook shared secret, resolved lazily and fail-closed.

        Lazy because a long-poll-only deployment never needs it, and asking the
        vault for a key that was never configured would turn a working simple
        tier into a startup failure. Fail-closed when it *is* asked for, for the
        reason in the module docstring.
        """
        resolved = _resolve_fail_closed(
            self._webhook_secret, WEBHOOK_SECRET_VAULT_KEY, self._environment
        )
        return resolved

    def __repr__(self) -> str:
        return f"<TelegramTransport name={self.name!r} token=***>"

    # -- tiers -------------------------------------------------------------
    def poll_once(
        self,
        *,
        offset: int | None = None,
        timeout: float | None = None,
        home: Any | None = None,
        db: str | None = None,
    ) -> PollResult:
        """One ``getUpdates`` tick: long-poll, then deliver each message found.

        ``offset`` is exclusive — Telegram acknowledges every update below it —
        so a caller threads :attr:`PollResult.next_offset` back in and a
        redelivery is impossible by construction. Each update is still delivered
        with its own ``external_id``, because the offset is *this process's*
        cursor: a second poller, or the webhook tier, has no view of it.

        The reply is an array, so one tick can carry several updates. Every one
        of them is delivered, and the batch is reported as a whole rather than
        only its last element: :func:`~mini_ork.integrations._base.merge_poll_results`
        keeps each update's own outcome on :attr:`PollResult.batch`, reports the
        last update that actually acted, and returns a cursor that clears the
        entire batch instead of the final element's id.
        """
        params: dict[str, Any] = {
            "timeout": int(timeout if timeout is not None else self._long_poll_seconds)
        }
        if offset is not None:
            params["offset"] = int(offset)
        payload = self._call("getUpdates", params)

        results = [
            self._ingest(update, home=home, db=db)
            for update in payload.get("result") or []
            if isinstance(update, Mapping)
        ]
        return merge_poll_results(results)

    def handle_webhook(
        self,
        body: bytes,
        header: str | None,
        *,
        home: Any | None = None,
        db: str | None = None,
        secret_token_header: str | None = None,
    ) -> Delivery | None:
        """Verify a pushed update and deliver it. ``None`` when it carried none.

        Two accepted proofs, both constant-time, and *neither* is optional when
        it is the one configured: the HMAC over the raw body via
        :func:`~mini_ork.intake.webhooks.verify` (the house scheme, shared with
        the GitHub tier), or Telegram's native ``X-Telegram-Bot-Api-Secret-Token``
        compared when the caller passes ``secret_token`` at construction. A body
        that satisfies neither raises :class:`TelegramSignatureError` — there is
        no local-caller escape hatch on this path, for the reason spelled out in
        :mod:`mini_ork.intake.webhooks`.

        The body is parsed only *after* it verifies: ``parse_payload`` turns
        remote bytes into a mapping or refuses, and doing it in that order means
        unauthenticated input is never handed to anything that indexes it.
        """
        if not self._verify(body, header, secret_token_header):
            raise TelegramSignatureError(
                f"{NAME}: the webhook did not carry a valid signature or secret "
                "token — refusing the update"
            )
        payload = webhooks.parse_payload(body)
        return self._ingest(payload, home=home, db=db).delivery

    def _verify(
        self, body: bytes, header: str | None, secret_token_header: str | None
    ) -> bool:
        """Whether either configured proof accepts ``(body, header)``."""
        # Telegram's native header is checked first only because it is the one a
        # real Bot API deployment will present; the HMAC remains the scheme a
        # relay in front of a non-Telegram surface uses.
        if self._secret_token:
            presented = (secret_token_header or "").strip()
            if presented and hmac.compare_digest(
                self._secret_token.strip(), presented
            ):
                return True
            # The endpoint was configured with a native secret token, and this
            # request did not present it (absent or wrong). That is a *signature*
            # failure, not a configuration one: fall through to the HMAC only
            # when a webhook secret is actually configured. Asking for one that
            # is absent would raise TelegramConfigError at a forged request,
            # naming "the secret is missing" for what is really "you are not the
            # sender" — fail-closed either way, but the wrong diagnosis.
            if not self._has_webhook_secret():
                return False
        return webhooks.verify(self.webhook_secret(), body, header)

    def _has_webhook_secret(self) -> bool:
        """Whether an HMAC webhook secret is configured — without raising.

        The probing twin of :meth:`webhook_secret`, which is fail-closed on
        purpose. Here the *answer* is "is there a second proof to try", and
        "no" is a legitimate result rather than a misconfiguration, so a vault
        that cannot resolve the key (or cannot be read at all) reports ``no``
        and the native-token decision stands.
        """
        if self._webhook_secret:
            return True
        try:
            environment = (
                vault_environment() if self._environment is None else self._environment
            )
            return bool(environment.resolve(WEBHOOK_SECRET_VAULT_KEY))
        except SecretStoreError:
            return False

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
        """Project an assistant turn onto a thread's transcript.

        The transcript is the transport-agnostic source of truth: a canvas, a
        Slack adapter and this transport all read the same ``thread_messages``
        rows, so what the assistant said is recorded once and delivered by
        whoever is watching. ``event_id`` makes the write idempotent — pass the
        run's own id (``f"{run_id}-final"`` is the convention
        :func:`~mini_ork.threads.service.on_run_terminal` uses) and a retry
        attaches to the same round instead of opening a second one.
        """
        return store.append_message(
            thread_id,
            role=MessageRole.ASSISTANT,
            text=text,
            turn=turn,
            event_id=event_id,
            db=self._target(home, db),
        )

    # -- internals ---------------------------------------------------------
    def _ingest(
        self, update: Mapping[str, Any], *, home: Any | None, db: str | None
    ) -> PollResult:
        """Deliver one Telegram update, or report it ignored with its id.

        An update with no message text is not an error and not a delivery: it
        still carries an ``update_id`` the cursor must pass, which is why the
        ``ignored`` result keeps it.
        """
        update_id = update.get("update_id")
        message = update.get("message") or update.get("edited_message") or {}
        if not isinstance(message, Mapping):
            message = {}
        text = str(message.get("text") or "")
        if not text.strip():
            return PollResult(action="ignored", update_id=update_id)

        chat = message.get("chat") or {}
        if not isinstance(chat, Mapping):
            chat = {}
        channel = chat.get("id", "")
        thread_id = thread_key(self.name, channel, message.get("message_thread_id"))

        delivery = service.deliver(
            self._home if home is None else home,
            thread_id,
            text,
            title=str(chat.get("title") or chat.get("username") or ""),
            origin=self._origin,
            origin_ref=f"{self.name}:{channel}",
            recipe=self._recipe,
            # Telegram redelivers an update until it is acknowledged, exactly as
            # GitHub does, so the same external_id dedupe the intake tier relies
            # on is what makes a redelivery a no-op here.
            external_id=f"{self.name}:{update_id}",
            source=self.name,
            db=self._target(home, db),
        )
        return PollResult(action=delivery.action, update_id=update_id, delivery=delivery)

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

    def _call(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """One Bot API GET, parsed. Errors never echo the URL.

        The request URL carries the bot token — that is the Bot API's own shape,
        not a choice made here — so a failure message must not repeat it. The
        exception is chained for a traceback in a debugger and the *message*
        names the method and the status, which is what a log line should hold.
        """
        url = f"{self._api_base}/bot{self._token}/{method}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self._http_timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise TelegramError(
                f"{NAME}: {method} failed with HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise TelegramError(
                f"{NAME}: {method} could not reach the Bot API: {exc.reason}"
            ) from exc

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramError(f"{NAME}: {method} returned a body that is not JSON") from exc
        if not isinstance(payload, Mapping) or not payload.get("ok"):
            description = ""
            if isinstance(payload, Mapping):
                description = str(payload.get("description") or "")
            raise TelegramError(
                f"{NAME}: {method} was refused by the Bot API: {description or 'no reason given'}"
            )
        return payload


__all__ = [
    "DEFAULT_API_BASE",
    "DEFAULT_LONG_POLL_SECONDS",
    "NAME",
    "SECRET_TOKEN_HEADER",
    "SIGNATURE_HEADER",
    "TOKEN_VAULT_KEY",
    "WEBHOOK_SECRET_VAULT_KEY",
    "TelegramConfigError",
    "TelegramError",
    "TelegramSignatureError",
    "TelegramTransport",
]
