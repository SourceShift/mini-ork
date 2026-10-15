"""Chat transports: a conversation on some surface, reduced to a thread.

The third external surface the threads primitive was built for. Work-intake
opens a thread from a ticket, attachments hang off one, and this package opens
one from a message on a chat surface — so the durable, multi-turn unit of work is
the same object no matter which door a message came through, and the state
machine that decides *launch, steer, continue or ignore* is written once
(:mod:`mini_ork.threads.service`) rather than per transport.

What is here:

``_base``
    The transport-free core — :func:`~mini_ork.integrations._base.thread_key`,
    :class:`~mini_ork.integrations._base.PollResult`, and the
    :class:`~mini_ork.integrations._base.Transport` protocol. Importing this
    package pulls it and every transport it re-exports; it pulls no session, no
    token and no network.

``telegram``
    :class:`~mini_ork.integrations.telegram.TelegramTransport` — a stdlib long
    poll for the simple tier and an HMAC-verified webhook for the secure tier,
    both over the same :func:`~mini_ork.threads.service.deliver` seam.

``slack``
    :class:`~mini_ork.integrations.slack.SlackTransport` — Socket Mode over the
    hand-written :mod:`~mini_ork.integrations._ws` client for the simple tier
    and an Events API webhook with Slack's ``v0`` signature for the secure tier,
    over the same :func:`~mini_ork.threads.service.deliver` seam.

**Importing this package is inert.** No token is resolved, no socket is opened,
no ``getUpdates`` is called: a transport is a *class* here, and the work starts
when one is constructed. That is deliberate — ``import mini_ork.integrations``
must be safe in a process that merely wants to name the type, and a module whose
import required a bot token would be unusable in exactly the test and the CLI
that most need to reach it.

Later transports follow the same seam: a module, a class with ``poll_once`` /
``handle_webhook`` / ``send``, and a re-export here. Nothing in this package is
Telegram-specific except the transport itself.
"""

from __future__ import annotations

from mini_ork.integrations._base import (
    PollResult,
    TRANSPORT_ORIGIN,
    Transport,
    thread_key,
)
from mini_ork.integrations.slack import (
    APP_TOKEN_VAULT_KEY as SLACK_APP_TOKEN_VAULT_KEY,
    BOT_TOKEN_VAULT_KEY as SLACK_BOT_TOKEN_VAULT_KEY,
    SIGNING_SECRET_VAULT_KEY as SLACK_SIGNING_SECRET_VAULT_KEY,
    SlackConfigError,
    SlackError,
    SlackSignatureError,
    SlackTransport,
)
from mini_ork.integrations.telegram import (
    NAME,
    TOKEN_VAULT_KEY,
    TelegramConfigError,
    TelegramError,
    TelegramSignatureError,
    TelegramTransport,
)

__all__ = [
    "NAME",
    "SLACK_APP_TOKEN_VAULT_KEY",
    "SLACK_BOT_TOKEN_VAULT_KEY",
    "SLACK_SIGNING_SECRET_VAULT_KEY",
    "TOKEN_VAULT_KEY",
    "PollResult",
    "SlackConfigError",
    "SlackError",
    "SlackSignatureError",
    "SlackTransport",
    "TRANSPORT_ORIGIN",
    "TelegramConfigError",
    "TelegramError",
    "TelegramSignatureError",
    "TelegramTransport",
    "Transport",
    "thread_key",
]
