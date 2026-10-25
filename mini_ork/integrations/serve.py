"""``mini-ork threads serve`` — the simple tier, wired to one config file.

A transport (:mod:`mini_ork.integrations.telegram`, ``.slack``, ``.discord``)
turns an inbound chat message into a thread through
:func:`mini_ork.threads.service.deliver`. This module is the glue that makes one
reachable from a laptop with no server: it reads ``$MINI_ORK_HOME/config/chat.yaml``,
resolves the transport's secrets from the vault by *name*, wraps the transport in
a channel allowlist, and loops on :meth:`poll_once` until Ctrl-C.

Three properties are load bearing, and every one of them is a security decision:

* **The file holds names, never values.** ``chat.yaml`` may only carry
  ``transport``, ``tier``, ``allow_channels`` and ``recipe``; any other key is
  refused outright, and no error message ever echoes a value. Secrets are
  resolved from the vault (:func:`mini_ork.dispatch.vault.vault_environment`) by
  key name and a missing key fails closed — never an empty fallback.
* **The allowlist is mandatory and default-deny.** Anyone who can message a bot
  could otherwise start runs on the operator's machine, so a channel outside
  ``allow_channels`` is reported ``ignored`` *before* ``service.deliver``: no
  thread row, no run, and (for Telegram) the cursor still advances.
* **Nothing happens at import.** No file is read, no vault consulted, no socket
  opened — a caller that merely wants the config helpers must be able to import
  this in a process that has no bot token at all.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import yaml

from mini_ork.dispatch.secrets import SecretStoreError
from mini_ork.dispatch.vault import vault_environment
from mini_ork.integrations import discord, slack, telegram
from mini_ork.integrations._base import PollResult

#: The environment variable that points at an explicit chat config file.
CONFIG_ENV = "MINI_ORK_CHAT"

#: Transports this module knows how to build, in the order a person meets them.
TRANSPORTS = ("telegram", "slack", "discord")

#: The two deployment tiers one config file selects between.
TIERS = ("simple", "secure")

#: The only keys ``chat.yaml`` may hold. Anything else is refused, so a secret
#: pasted into the file is a loud error rather than a silently ignored value.
ALLOWED_KEYS = frozenset({"transport", "tier", "allow_channels", "recipe"})

#: Env-safe names for Telegram's secrets. The transport's own
#: :data:`~mini_ork.integrations.telegram.TOKEN_VAULT_KEY` is dotted, which
#: neither ``secrets.local.sh`` nor a shell export can hold, so this module
#: resolves these names itself and passes the values in explicitly.
TELEGRAM_BOT_TOKEN_KEY = "TELEGRAM_BOT_TOKEN"
TELEGRAM_WEBHOOK_SECRET_KEY = "TELEGRAM_WEBHOOK_SECRET"

#: Which vault keys each ``(transport, tier)`` pair needs. Every one of them is
#: resolved before a transport is constructed; a hole stops the loop.
SECRET_KEYS: dict[tuple[str, str], tuple[str, ...]] = {
    ("telegram", "simple"): (TELEGRAM_BOT_TOKEN_KEY,),
    ("telegram", "secure"): (TELEGRAM_BOT_TOKEN_KEY, TELEGRAM_WEBHOOK_SECRET_KEY),
    ("slack", "simple"): (slack.BOT_TOKEN_VAULT_KEY, slack.APP_TOKEN_VAULT_KEY),
    ("slack", "secure"): (slack.BOT_TOKEN_VAULT_KEY, slack.SIGNING_SECRET_VAULT_KEY),
    ("discord", "simple"): (discord.BOT_TOKEN_VAULT_KEY,),
    ("discord", "secure"): (discord.BOT_TOKEN_VAULT_KEY, discord.PUBLIC_KEY_VAULT_KEY),
}

#: The simple tier's websocket idle timeout for Slack/Discord. The transports
#: default to 30 s, which is a reconnect per idle half-minute; a poll loop wants
#: to sit on the socket instead.
SOCKET_IDLE_SECONDS = 600.0

#: Slack's per-tick envelope budget. The default ``limit=1`` reads the ``hello``
#: frame and never reaches a message, so a loop would tick forever and deliver
#: nothing.
SOCKET_FRAMES_PER_TICK = 10_000

#: Exponential backoff for a failing tick, and the ceiling it doubles toward.
BACKOFF_START = 1.0
BACKOFF_CAP = 60.0

_DISCORD_LOOP_REFUSAL = (
    "chat: the Discord Gateway tier opens a new session (IDENTIFY) per tick and "
    "cannot RESUME — looping it spends the bot's daily session-start budget and "
    "drops messages between ticks. Use tier: secure (the Interactions webhook), "
    "or --once for a single probe."
)


class ChatConfigError(RuntimeError):
    """A chat setting is missing or unusable.

    Messages name a path, key or setting — never a secret's value: these strings
    reach stderr and logs, and the whole point of the vault indirection is that
    the value never does.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class ChatConfig:
    """One parsed ``chat.yaml``. Holds names and ids only, never a secret."""

    transport: str
    tier: str
    allow_channels: frozenset[str]
    recipe: str | None
    path: Path


def config_path(home: Any, environ: Mapping[str, str] | None = None) -> Path:
    """The config file for ``home``: ``$MINI_ORK_CHAT``, else ``home/config/chat.yaml``."""
    env = os.environ if environ is None else environ
    explicit = str(env.get(CONFIG_ENV) or "").strip()
    if explicit:
        return Path(explicit)
    return Path(home) / "config" / "chat.yaml"


def load_chat_config(
    home: Any, environ: Mapping[str, str] | None = None
) -> ChatConfig | None:
    """Read and validate ``chat.yaml``. ``None`` only when there is no file and
    no explicit path was asked for.

    The whole document is validated before anything is built, so a typo is a
    startup error rather than a surprise on the first inbound message.
    """
    env = os.environ if environ is None else environ
    path = config_path(home, env)
    explicit = str(env.get(CONFIG_ENV) or "").strip()

    if not path.exists():
        if explicit:
            raise ChatConfigError(f"chat config {path} does not exist")
        return None

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ChatConfigError(f"chat config {path} cannot be read: {exc}") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ChatConfigError(f"chat config {path} is not valid YAML: {exc}") from exc

    if not isinstance(document, Mapping):
        raise ChatConfigError(f"chat config {path} must be a mapping of settings")

    for key in document:
        if key not in ALLOWED_KEYS:
            raise ChatConfigError(
                f"{path}: unknown key {key!r} — chat.yaml holds names, never "
                "secrets; secrets live in the vault"
            )

    transport = document.get("transport")
    if transport not in TRANSPORTS:
        raise ChatConfigError(
            f"{path}: transport must be one of {', '.join(TRANSPORTS)}"
        )
    tier = document.get("tier")
    if tier not in TIERS:
        raise ChatConfigError(f"{path}: tier must be one of {', '.join(TIERS)}")

    raw_channels = document.get("allow_channels")
    if not isinstance(raw_channels, list) or not raw_channels:
        raise ChatConfigError(
            f"{path}: allow_channels is required and must name at least one "
            "chat/channel id — chat never accepts messages from everywhere"
        )
    channels: set[str] = set()
    for item in raw_channels:
        channel = str(item).strip()
        if not channel:
            raise ChatConfigError(
                f"{path}: allow_channels is required and must name at least one "
                "chat/channel id — chat never accepts messages from everywhere"
            )
        channels.add(channel)

    raw_recipe = document.get("recipe")
    recipe = raw_recipe.strip() if isinstance(raw_recipe, str) else ""
    return ChatConfig(
        transport=transport,
        tier=tier,
        allow_channels=frozenset(channels),
        recipe=recipe or None,
        path=path,
    )


def resolve_secrets(
    config: ChatConfig, environment: Any | None = None
) -> dict[str, str]:
    """Resolve every vault key this ``(transport, tier)`` needs.

    The environment is the vault chain unless one is supplied (the seam tests
    use). A key that is absent or empty is a hard error: the transport must not
    be built with a blank token, and the message names the key, never a value.
    """
    if environment is None:
        try:
            env = vault_environment()
        except SecretStoreError as exc:
            raise ChatConfigError(f"chat: the secret vault is unusable: {exc}") from exc
    else:
        env = environment

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for key in SECRET_KEYS[(config.transport, config.tier)]:
        try:
            value = env.resolve(key)
        except SecretStoreError as exc:
            raise ChatConfigError(
                f"chat: the secret vault is unusable while resolving {key}: {exc}"
            ) from exc
        if value is None or value == "":
            missing.append(key)
        else:
            resolved[key] = value

    if missing:
        raise ChatConfigError(
            f"chat: no value for {', '.join(missing)} from {env.describe()} — add "
            "it to the vault (see `mini-ork vault which <KEY>`); chat never falls "
            "back to an empty secret"
        )
    return resolved


class _GatedTelegram(telegram.TelegramTransport):
    """A Telegram transport that only admits the configured chats."""

    def __init__(self, *, allow: frozenset[str], **kwargs: Any) -> None:
        self._allow = frozenset(allow)
        super().__init__(**kwargs)

    def _ingest(
        self, update: Mapping[str, Any], *, home: Any | None, db: str | None
    ) -> PollResult:
        update_id = update.get("update_id")
        message = update.get("message") or update.get("edited_message")
        channel = None
        if isinstance(message, Mapping):
            chat = message.get("chat")
            if isinstance(chat, Mapping):
                channel = chat.get("id")
        if str(channel) not in self._allow:
            # Keep the update id: the cursor still has to clear a denied update
            # or the long poll re-reads it forever.
            return PollResult(action="ignored", update_id=update_id)
        return super()._ingest(update, home=home, db=db)


class _GatedSlack(slack.SlackTransport):
    """A Slack transport that only admits the configured channels."""

    def __init__(self, *, allow: frozenset[str], **kwargs: Any) -> None:
        self._allow = frozenset(allow)
        super().__init__(**kwargs)

    def _ingest(
        self, body: Mapping[str, Any], *, home: Any | None, db: str | None
    ) -> PollResult:
        channel = None
        if isinstance(body, Mapping):
            event = body.get("event")
            if isinstance(event, Mapping):
                channel = event.get("channel")
        if str(channel) not in self._allow:
            return PollResult(action="ignored")
        return super()._ingest(body, home=home, db=db)


class _GatedDiscord(discord.DiscordTransport):
    """A Discord transport that only admits the configured channels.

    Every inbound path — Gateway dispatch and signed interaction — funnels
    through ``_deliver``, so gating here covers both tiers.
    """

    def __init__(self, *, allow: frozenset[str], **kwargs: Any) -> None:
        self._allow = frozenset(allow)
        super().__init__(**kwargs)

    def _deliver(
        self,
        payload: Mapping[str, Any],
        *,
        channel: str,
        text: str,
        home: Any | None,
        db: str | None,
    ) -> PollResult:
        if str(channel) not in self._allow:
            return PollResult(action="ignored")
        return super()._deliver(payload, channel=channel, text=text, home=home, db=db)


def build_transport(
    config: ChatConfig,
    *,
    environment: Any | None = None,
    home: Any | None = None,
    db: str | None = None,
) -> Any:
    """Construct the gated transport ``config`` describes.

    The vault is consulted once, every secret is resolved up front, and the
    values are passed in explicitly. That is what keeps a missing key a startup
    error (before any socket) instead of a lazy failure on the first tick.
    """
    if environment is None:
        try:
            env = vault_environment()
        except SecretStoreError as exc:
            raise ChatConfigError(f"chat: the secret vault is unusable: {exc}") from exc
    else:
        env = environment

    secrets = resolve_secrets(config, env)
    common: dict[str, Any] = {
        "allow": config.allow_channels,
        "environment": env,
        "recipe": config.recipe,
        "home": home,
        "db": db,
    }

    try:
        if config.transport == "telegram":
            return _GatedTelegram(
                token=secrets[TELEGRAM_BOT_TOKEN_KEY],
                webhook_secret=secrets.get(TELEGRAM_WEBHOOK_SECRET_KEY),
                secret_token=secrets.get(TELEGRAM_WEBHOOK_SECRET_KEY),
                **common,
            )
        if config.transport == "slack":
            if config.tier == "simple":
                common["ws_timeout"] = SOCKET_IDLE_SECONDS
            return _GatedSlack(
                bot_token=secrets[slack.BOT_TOKEN_VAULT_KEY],
                app_token=secrets.get(slack.APP_TOKEN_VAULT_KEY),
                signing_secret=secrets.get(slack.SIGNING_SECRET_VAULT_KEY),
                **common,
            )
        if config.tier == "simple":
            common["ws_timeout"] = SOCKET_IDLE_SECONDS
        return _GatedDiscord(
            bot_token=secrets[discord.BOT_TOKEN_VAULT_KEY],
            public_key=secrets.get(discord.PUBLIC_KEY_VAULT_KEY),
            **common,
        )
    except (
        telegram.TelegramConfigError,
        slack.SlackConfigError,
        discord.DiscordConfigError,
    ) as exc:
        raise ChatConfigError(str(exc)) from exc


def serve(
    transport: Any,
    *,
    home: Any,
    db: str | None,
    once: bool = False,
    sleep: Any = time.sleep,
    out: Any | None = None,
    err: Any | None = None,
) -> int:
    """Poll ``transport`` forever (or once) and report what was delivered.

    A failed tick backs off exponentially; a timeout is silent (the transport
    was merely idle); Ctrl-C stops cleanly. Discord is refused without ``once``
    because every Gateway tick is a fresh IDENTIFY that cannot RESUME.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    name = str(transport.name)

    if name == "discord" and not once:
        err.write(_DISCORD_LOOP_REFUSAL + "\n")
        return 2

    err.write(f"chat: serving {name} (simple tier) into {home} — Ctrl-C to stop\n")

    cursor: int | None = None
    backoff = BACKOFF_START
    while True:
        try:
            if name == "telegram":
                result = transport.poll_once(offset=cursor, home=home, db=db)
            elif name == "slack":
                result = transport.poll_once(
                    home=home, db=db, limit=SOCKET_FRAMES_PER_TICK
                )
            else:
                result = transport.poll_once(home=home, db=db)
        except TimeoutError:
            # An idle long poll, not a fault: no backoff, no noise.
            if once:
                return 0
            continue
        except KeyboardInterrupt:
            err.write("chat: stopped\n")
            return 0
        except Exception as exc:  # noqa: BLE001 — one bad tick must not kill a loop
            err.write(f"chat: {name} tick failed: {type(exc).__name__}: {exc}\n")
            if once:
                return 1
            sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_CAP)
            continue

        if result.next_offset is not None:
            cursor = result.next_offset
        for item in result.batch or (result,):
            if item.action != "ignored" and item.delivery is not None:
                out.write(
                    f"{item.action}: thread {item.delivery.thread_id} "
                    f"run {item.delivery.run_id or '-'}\n"
                )
        backoff = BACKOFF_START
        if once:
            return 0


__all__ = [
    "ALLOWED_KEYS",
    "BACKOFF_CAP",
    "BACKOFF_START",
    "CONFIG_ENV",
    "ChatConfig",
    "ChatConfigError",
    "SECRET_KEYS",
    "SOCKET_FRAMES_PER_TICK",
    "SOCKET_IDLE_SECONDS",
    "TELEGRAM_BOT_TOKEN_KEY",
    "TELEGRAM_WEBHOOK_SECRET_KEY",
    "TIERS",
    "TRANSPORTS",
    "build_transport",
    "config_path",
    "load_chat_config",
    "resolve_secrets",
    "serve",
]
