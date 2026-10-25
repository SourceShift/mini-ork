# ECOSYSTEM §6 · K15b: chat secure tier — `POST /api/v1/chat/<transport>` webhook routes

## Goal

eco6 K15a added `mini_ork/integrations/serve.py` (config, allowlist-gated
transports, `build_transport`) and the simple tier (`mini-ork threads serve`).
This kickoff adds the **secure tier**: one webhook route per transport in the
existing web app, verified by the transport's own signature check (Telegram
secret token / HMAC, Slack v0 signing with a 300 s window, Discord Ed25519),
enabled only when `config/chat.yaml` says `tier: secure` for that transport.
Fail closed: a missing secret or a vault protection gap answers 503, a bad
signature 401, and neither writes a `threads` row.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/web/routes/chat.py` (new)

Follow `mini_ork/web/routes/intake.py` (read it first: its home/db resolution,
`webhooks.MAX_BODY_BYTES`, `webhooks.protection_gap()`). No
`require_local_caller` / `require_token`: the platform signature is the guard,
as in intake.

`router = APIRouter(prefix="/api/v1/chat", tags=["chat"])`; helpers
`_json(payload, status=200)` and `_refuse(status, error)` →
`{"ok": False, "error": error}`.

`async def _prepare(name, request)` → `(transport, home, db, body)` or a
`JSONResponse`, checking in this order:
1. `home = <intake's default-home helper>()`;
   `config = chat.load_chat_config(home)`; `ChatConfigError` → log to stderr,
   `_refuse(503, "chat is misconfigured on this server")`.
2. `config is None` or `config.tier != "secure"` or `config.transport != name`
   → `_refuse(404, f"chat webhooks for {name!r} are not enabled on this server")`.
3. `body = await request.body()`; `len(body) > webhooks.MAX_BODY_BYTES` → 413.
4. `gap = webhooks.protection_gap()`; a gap → `_refuse(503, gap)`.
5. `db = <intake's db resolver>(home)`;
   `transport = chat.build_transport(config, home=home, db=db)`;
   `ChatConfigError` → log it to stderr (it names the vault key) and
   `_refuse(503, f"chat is not configured to accept {name!r} webhooks")`.

`POST /telegram`: `transport.handle_webhook(body, request.headers.get(telegram.SIGNATURE_HEADER), secret_token_header=request.headers.get(telegram.SECRET_TOKEN_HEADER), home=home, db=db)`.
`TelegramSignatureError` → 401 `"the signature does not match"`;
`TelegramConfigError` → 503; `ValueError` → 400. `None` →
`{"ok": True, "action": "ignored"}`; a delivery `d` →
`{"ok": d.ok, **d.to_dict()}` with 200 even when `action == "error"` (no
platform retry storm).

`POST /slack`: `ts = request.headers.get(slack.TIMESTAMP_HEADER)`,
`sig = request.headers.get(slack.SIGNATURE_HEADER)`; first
`transport.webhook_challenge(body, sig, timestamp=ts)` → `{"challenge": …}` when
it returns one; else `transport.handle_webhook(body, sig, timestamp=ts, home=home, db=db)`;
errors and successes as Telegram with `SlackSignatureError` / `SlackConfigError`.

`POST /discord`: `result = transport.handle_webhook(request.headers, body, home=home, db=db)`.
`DiscordSignatureError` → 401; `DiscordConfigError` → 503; `ValueError` → 400.
A `dict` (PONG) → return it unchanged; `None` →
`{"type": 4, "data": {"content": "nothing to deliver", "flags": 64}}`; a
delivery `d` →
`{"type": 4, "data": {"content": f"{d.action}: thread {d.thread_id}" + (f", run {d.run_id}" if d.run_id else ""), "flags": 64}}`.

Match the real names in the transport modules (header constants, error
classes, `handle_webhook` / `webhook_challenge` signatures); read them first.

### 2. `mini_ork/web/app.py`

Add `chat as chat_routes` to the routes import block and
`app.include_router(chat_routes.router)` right after the intake router, with a
two-line comment: chat webhooks are enabled by `config/chat.yaml`
`tier: secure`, they are platform-signed, and `/api/` is outside the SPA
fallback.

## Files in scope (touch ONLY these)

- `mini_ork/web/routes/chat.py` (new)
- `mini_ork/web/app.py`
- `tests/unit/test_chat_webhook_routes.py` (new)

Do NOT modify `mini_ork/integrations/*`, `mini_ork/threads/*` or the docs
(K15a already documented the secure tier).

## Tests (`tests/unit/test_chat_webhook_routes.py`, hermetic)

Fixtures as in `tests/unit/test_chat_serve.py` (vault stub via
`serve.vault_environment`, launch/steer recorders, migrated `home`).
`TestClient(create_app(home=home, dev_cors=False))` (match the real factory
signature); monkeypatch the route module's `webhooks.protection_gap` to
`lambda: None` except in the 503-gap test. For Discord, monkeypatch
`discord.DiscordTransport.verify_ed25519` (or the real verify function) to raise
`DiscordSignatureError` unless the signature header is `"good"`.

1. `POST /api/v1/chat/telegram` → 404 with no chat.yaml; with `tier: simple`;
   with another transport configured. A body over `MAX_BODY_BYTES` → 413.
2. Telegram secure refusals — each leaves no thread row and no launch: a
   protection gap → 503; `TELEGRAM_WEBHOOK_SECRET` missing → 503; a wrong
   `x-telegram-bot-api-secret-token` → 401.
3. Telegram secure, valid secret token: first post → 200 `launched`, thread
   `telegram-4242` with origin `chat`, one launch; the same body again → 200
   `duplicate`, still one launch; a denied chat → 200 `ignored`, no row.
4. Slack: a signed `url_verification` → `{"challenge": …}`; a signed
   `event_callback` message from an allowed channel → 200 `launched`; a
   timestamp 10 000 s old → 401, no row.
5. Discord: PING with signature `good` → `{"type": 1}`; an
   APPLICATION_COMMAND with option "fix the build" → `type == 4`, content starts
   `launched`; a bad signature → 401, no row; missing `DISCORD_PUBLIC_KEY` → 503.

## Verification commands

- `env -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_chat_webhook_routes.py tests/unit/test_chat_serve.py tests/unit/test_web_intake_route.py tests/test_web_smoke.py`
- `ruff check --select F,E9 mini_ork/web/routes/chat.py mini_ork/web/app.py`

## Self-application measurement

```measure
env -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_chat_webhook_routes.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k15b-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k15b-junit.xml`

## Success criteria

- Both verification commands pass; every pre-existing test unchanged.
- A bad signature never writes a `threads` row; a missing secret answers 503.
- No secret value appears in any HTTP response or log line from the new code.
