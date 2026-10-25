"""``POST /api/v1/chat/<transport>`` — the secure tier's inbound webhooks.

K15a built the transports and the simple tier (``mini-ork threads serve``); this
module is the HTTP shell that lets a platform *push* to mini-ork instead. One
route per transport, enabled only when ``config/chat.yaml`` says ``tier:
secure`` for that transport. The route owns status codes and nothing else: the
signature schemes, the allowlist gate and the delivery state machine all live in
:mod:`mini_ork.integrations`, where a test can reach them without a server.

Three things this route deliberately does *not* do.

* It does not call :func:`~mini_ork.web.routes.agent_server.require_local_caller`.
  A webhook is always foreign-origin, so the local-caller guard would refuse
  every legitimate call and close the exact threat it is not meant for. The
  platform's signature is the guard, exactly as on the intake route.
* It does not parse the body before the transport verifies it. Each
  ``handle_webhook`` verifies the raw bytes first and parses second, so
  unauthenticated input never reaches a parser.
* It does not echo a configuration error. ``ChatConfigError`` messages name
  vault keys, which is operator information for stderr — the sender gets a
  fixed sentence, and a missing secret is a 503 with no ``threads`` row.

A switched-off transport answers **404, not 403**: an endpoint that is not
enabled should look absent, and a 403 would tell a scanner which server has the
tier installed.
"""

from __future__ import annotations

import sys

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mini_ork.intake import webhooks
from mini_ork.integrations import discord, serve as chat, slack, telegram

from ..db import resolve_db
from ..deps import get_default_home

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


def _json(payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status)


def _refuse(status: int, error: str) -> JSONResponse:
    return _json({"ok": False, "error": error}, status=status)


def _ignored() -> JSONResponse:
    """Authentic, but nothing to deliver — never an error to the platform."""
    return _json({"ok": True, "action": "ignored"})


async def _prepare(name: str, request: Request):
    """The five checks every transport shares, in the order the kickoff fixes.

    Returns ``(transport, home, db, body)`` or the refusal ``JSONResponse``.
    The order is load-bearing: a server with no ``chat.yaml`` answers 404 even
    for an oversized body, and the vault's protection gap is consulted before
    any secret is resolved.
    """
    home = get_default_home()

    try:
        config = chat.load_chat_config(home)
    except chat.ChatConfigError as exc:
        # The message names a path or key, never a secret's value.
        sys.stderr.write(f"chat webhook: {exc}\n")
        return _refuse(503, "chat is misconfigured on this server")

    if config is None or config.tier != "secure" or config.transport != name:
        return _refuse(
            404, f"chat webhooks for {name!r} are not enabled on this server"
        )

    body = await request.body()
    if len(body) > webhooks.MAX_BODY_BYTES:
        return _refuse(413, "the body is larger than this route accepts")

    gap = webhooks.protection_gap()
    if gap is not None:
        return _refuse(503, gap)

    db = resolve_db(home)
    try:
        transport = chat.build_transport(config, home=home, db=db)
    except chat.ChatConfigError as exc:
        # Names the missing key; operator information for stderr only.
        sys.stderr.write(f"chat webhook: {exc}\n")
        return _refuse(
            503, f"chat is not configured to accept {name!r} webhooks"
        )

    return transport, home, db, body


@router.post("/telegram")
async def telegram_webhook(request: Request) -> JSONResponse:
    """One Telegram update, verified by its secret token or HMAC."""
    prepared = await _prepare("telegram", request)
    if isinstance(prepared, JSONResponse):
        return prepared
    transport, home, db, body = prepared

    try:
        delivery = transport.handle_webhook(
            body,
            request.headers.get(telegram.SIGNATURE_HEADER),
            secret_token_header=request.headers.get(telegram.SECRET_TOKEN_HEADER),
            home=home,
            db=db,
        )
    except telegram.TelegramSignatureError:
        return _refuse(401, "the signature does not match")
    except telegram.TelegramConfigError as exc:
        sys.stderr.write(f"chat webhook: {exc}\n")
        return _refuse(503, "chat is not configured to accept 'telegram' webhooks")
    except ValueError as exc:
        return _refuse(400, str(exc))

    if delivery is None:
        return _ignored()
    return _json({"ok": delivery.ok, **delivery.to_dict()})


@router.post("/slack")
async def slack_webhook(request: Request) -> JSONResponse:
    """One Slack Events API delivery — a message, or the URL handshake."""
    prepared = await _prepare("slack", request)
    if isinstance(prepared, JSONResponse):
        return prepared
    transport, home, db, body = prepared

    timestamp = request.headers.get(slack.TIMESTAMP_HEADER)
    signature = request.headers.get(slack.SIGNATURE_HEADER)

    try:
        # The handshake is verified by the same call as a delivery, so the
        # challenge path must sit inside this try — a forged handshake raises
        # SlackSignatureError here, before handle_webhook is ever reached.
        challenge = transport.webhook_challenge(
            body, signature, timestamp=timestamp
        )
        if challenge:
            return _json({"challenge": challenge})
        delivery = transport.handle_webhook(
            body, signature, timestamp=timestamp, home=home, db=db
        )
    except slack.SlackSignatureError:
        return _refuse(401, "the signature does not match")
    except slack.SlackConfigError as exc:
        sys.stderr.write(f"chat webhook: {exc}\n")
        return _refuse(503, "chat is not configured to accept 'slack' webhooks")
    except ValueError as exc:
        return _refuse(400, str(exc))

    if delivery is None:
        return _ignored()
    return _json({"ok": delivery.ok, **delivery.to_dict()})


@router.post("/discord")
async def discord_webhook(request: Request) -> JSONResponse:
    """One Discord interaction — the PING handshake, or a command."""
    prepared = await _prepare("discord", request)
    if isinstance(prepared, JSONResponse):
        return prepared
    transport, home, db, body = prepared

    try:
        result = transport.handle_webhook(
            request.headers, body, home=home, db=db
        )
    except discord.DiscordSignatureError:
        return _refuse(401, "the signature does not match")
    except discord.DiscordConfigError as exc:
        sys.stderr.write(f"chat webhook: {exc}\n")
        return _refuse(503, "chat is not configured to accept 'discord' webhooks")
    except ValueError as exc:
        return _refuse(400, str(exc))

    # A PONG is a control message, not a turn: return the transport's dict as
    # Discord shaped it.
    if isinstance(result, dict):
        return _json(result)
    if result is None:
        return _json(
            {"type": 4, "data": {"content": "nothing to deliver", "flags": 64}}
        )

    content = f"{result.action}: thread {result.thread_id}"
    if result.run_id:
        content += f", run {result.run_id}"
    return _json({"type": 4, "data": {"content": content, "flags": 64}})
