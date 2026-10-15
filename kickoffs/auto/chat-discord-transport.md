# Discord transport over the thread seam

## Goal

Let a Discord conversation drive a mini-ork thread the way Telegram and Slack do. Mirror
`mini_ork/integrations/slack.py` and reuse its WebSocket client `mini_ork/integrations/_ws.py`
(landed with the Slack transport) — do not write a second one.

**Deliverable.** `mini_ork/integrations/discord.py` (`DiscordTransport`), registration in
`mini_ork/integrations/__init__.py`, an optional extra in `pyproject.toml`, and
`tests/unit/test_integrations_discord.py`.

**Scope:**
- **Simple tier — Gateway (no public URL).** `GET /gateway/bot` with the bot token over stdlib
  `urllib` → connect through `_ws.py` → on HELLO start heartbeating at its interval (send op 1
  with the last sequence number; a missed heartbeat ACK closes and reports, it never spins) →
  IDENTIFY with intents `GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT` → for each
  `MESSAGE_CREATE` from a non-bot author, deliver its content to
  `thread_key("discord", channel_id, <thread or channel id>)` with origin `CHAT`. A replayed
  message id is a no-op through the thread store's `external_id` dedupe. Bounded
  `poll_once`-style method for tests.
- **Secure tier — Interactions endpoint.** `handle_webhook(headers, body)` verifies
  `X-Signature-Ed25519` over `X-Signature-Timestamp + raw body` with the application's public
  key. Ed25519 is not in the stdlib: use `cryptography` behind a new optional extra
  `integrations = ["cryptography>=42"]` in `pyproject.toml`, imported lazily. Without it the
  webhook **refuses** with an error naming `pip install 'mini-ork[integrations]'` — never an
  unverified accept. Answer a PING (type 1) with PONG; route an application-command or message
  interaction's text through the same delivery as the Gateway.
- **`send(thread_id, text)`** → `POST /channels/{channel_id}/messages` with the bot token.
- **Secrets — fail closed.** Vault key names `DISCORD_BOT_TOKEN` and `DISCORD_PUBLIC_KEY`; a
  missing one raises naming the key; no token in `__repr__`, a log line or an exception message.
  The required runtime dependency list stays `PyYAML` + `jsonschema`.

**Acceptance.** Offline — `urllib` monkeypatched and the Gateway driven through a fake `_ws`
connection:
- HELLO starts the heartbeat at the given interval and IDENTIFY carries the three intents;
- a first user message opens a thread (`launched`); a second in the same channel/thread is
  `steered` or `continued`, never a second concurrent run; a bot author is ignored; a replayed
  message id is a no-op;
- Interactions: with `cryptography` present a valid signature is accepted and a forged one
  refused, PING returns PONG; with it absent (monkeypatched import failure) the webhook refuses
  and names the extra;
- a missing `DISCORD_BOT_TOKEN` raises naming the key; no token in any raised message.

- max attempts: 2

---

## Scope Hint

- `mini_ork/integrations/discord.py`
- `mini_ork/integrations/__init__.py`
- `pyproject.toml`
- `tests/unit/test_integrations_discord.py`
- `mini_ork/integrations/_ws.py` — read-only: reuse it
- `mini_ork/integrations/slack.py` — read-only reference: the shape to mirror

## Verification commands
- `python3.11 -c "import mini_ork.integrations.discord"`
- `python3.11 -m pytest -q tests/unit/test_integrations_discord.py tests/unit/test_integrations_slack.py tests/unit/test_integrations_ws.py`

Run these from the run's working tree (the isolated worktree), so `mini_ork` resolves to the tree under test. A syntax check is not acceptance.

## Done When

- `${MINI_ORK_RUN_DIR}/panel-verdict.json` contains `"verdict": "pass"` with all verifiers passing.
- All verification commands pass in the isolated worktree.
