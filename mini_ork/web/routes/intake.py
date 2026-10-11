"""``POST /api/v1/intake/{source}`` — the inbound half of intake, tier B.

The HTTP shell around :mod:`mini_ork.intake.webhooks`: this module owns status
codes and message bodies, that one owns the HMAC and the decision. Nothing here
is clever, which is the intent — every line of security lives in the module a
test can call without a server.

Three things this route deliberately does *not* do.

* It does not call :func:`~mini_ork.web.routes.agent_server.require_local_caller`.
  That guard refuses a browser page not served from this machine, which is the
  right control for a tokenless route a tab can reach by accident. A webhook is
  the opposite case: it is always foreign, so the guard would refuse every
  legitimate call, and it is never a browser, so the threat the guard closes is
  not the threat. HMAC is mandatory here and there is no escape from it.
* It does not trust the payload's *content*. The body names an issue; the issue
  itself is fetched from the source, so the ticket the agent reads is the one
  the source serves now. A signed payload is authentic, not current.
* It does not fall back to polling's ledger for replay. It writes the same
  ``intake_events`` row, keyed by the provider's own delivery id, so a
  redelivery is a no-op and the two tiers share one record of what was handled.

**A redelivery returns 200, not 4xx.** GitHub retries until it sees a 2xx, and a
retry it should stop making is exactly what "already handled" is — answering 409
would make it retry forever against a ticket that is already running.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mini_ork.intake import webhooks
from mini_ork.intake.dispatch import dispatch_issue
from mini_ork.intake.poll import trigger_for
from mini_ork.intake.trigger import matches_trigger
from mini_ork.pm import config as pm_config
from mini_ork.pm import registry
from mini_ork.pm.provider import ProviderError
from mini_ork.threads import store as thread_store

from ..db import resolve_db
from ..deps import get_default_home

router = APIRouter(prefix="/api/v1", tags=["intake"])


def _json(payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status)


def _ignored(reason: str) -> JSONResponse:
    """A payload that was authentic but needed no action. Never an error."""
    return _json({"ok": True, "action": "ignored", "detail": reason})


def _says(reason: str) -> JSONResponse:
    """A payload that needed no action *and* the sender may want to know why."""
    return _json({"ok": True, "action": "skipped", "detail": reason})


def _refuse(status: int, error: str) -> JSONResponse:
    return _json({"ok": False, "error": error}, status=status)


@router.post("/intake/{source}")
async def receive(source: str, request: Request) -> JSONResponse:
    """One webhook delivery, from signature to dispatch."""
    if not webhooks.webhooks_enabled():
        # 404, not 403: an endpoint that is switched off should look absent.
        # A 403 would tell a scanner which servers have the tier installed.
        return _refuse(404, "intake webhooks are not enabled on this server")

    body = await request.body()
    if len(body) > webhooks.MAX_BODY_BYTES:
        return _refuse(413, "the body is larger than this route accepts")

    # ── the deployment has to be able to keep a shared secret ─────────────
    gap = webhooks.protection_gap()
    if gap is not None:
        return _refuse(503, gap)

    # ── the source has to exist, be declared, and hold a secret ───────────
    try:
        configuration = pm_config.load_config()
        if source not in configuration.sources:
            return _refuse(404, f"no source {source!r} is declared in config/pm.yaml")
        provider = registry.get_provider(source)
    except registry.UnknownProvider:
        return _refuse(404, f"no adapter is built for source {source!r}")
    except pm_config.PmConfigError as exc:
        sys.stderr.write(f"intake webhook: {exc}\n")
        return _refuse(500, "the project-management config could not be read")

    if not isinstance(provider, webhooks.WebhookSource):
        return _refuse(404, f"source {source!r} does not take webhooks")

    try:
        secret = webhooks.webhook_secret(source, config=configuration)
    except ProviderError as exc:
        # The reason names the vault key, which is operator information and not
        # the sender's business. It goes to the server's own stderr.
        sys.stderr.write(f"intake webhook: {exc}\n")
        return _refuse(503, f"intake is not configured to accept {source!r} webhooks")

    if not webhooks.verify(secret, body, request.headers.get(webhooks.SIGNATURE_HEADER)):
        # One message for a missing header, an unknown algorithm and a wrong
        # digest alike, so a failed check says nothing about which part failed.
        return _refuse(401, "the signature does not match")

    # ── from here the body is authentic; it is still not trusted ───────────
    try:
        payload = webhooks.parse_payload(body)
    except ValueError as exc:
        return _refuse(400, str(exc))

    delivery_id = (request.headers.get(webhooks.DELIVERY_HEADER) or "").strip()

    # One home, one database, resolved once. The replay probe and the ledger
    # write must read the same file, and `store`'s default resolution (env, then
    # a cwd-relative path) is not the server's — passing the same `db` to both
    # is what keeps "already handled" from being answered by a different home.
    home: Path = get_default_home()
    db = resolve_db(home)

    if delivery_id and thread_store.intake_event(source, delivery_id, db=db) is not None:
        return _ignored("this delivery has already been handled")

    uid = provider.uid_from_webhook(payload)
    if uid is None:
        event = request.headers.get(webhooks.EVENT_HEADER) or "this event"
        return _ignored(f"{event} names no issue for {source!r}")

    try:
        issue = provider.get(uid)
    except KeyError as exc:
        # Deleted, or a pull request. Either way there is nothing to work.
        return _ignored(f"{uid} is not a readable issue: {exc}")
    except ProviderError as exc:
        sys.stderr.write(f"intake webhook: {exc}\n")
        return _refuse(502, "the source could not be read")

    rules = trigger_for(source, config=configuration)
    matched, reason = matches_trigger(issue, rules)
    if not matched:
        return _says(reason)
    if not issue.is_open:
        return _says(f"the source reports it as {issue.state}")

    home: Path = get_default_home()
    db = resolve_db(home)

    dispatch = dispatch_issue(
        home,
        provider,
        issue,
        recipe=rules.recipe,
        # The provider's own delivery id: it is a better key than
        # `uid@updated_at` because it is what GitHub itself retries on.
        delivery_id=delivery_id or None,
        source="intake-webhook",
        db=db,
    )
    return _json({"ok": True, **dispatch.to_dict()})
