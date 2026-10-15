# Slack transport over the thread seam

## Goal

Let a Slack conversation drive a mini-ork thread exactly as Telegram does: an inbound message
resolves to a thread and runs `threads.service.deliver`, and replies go back into the same Slack
thread. Telegram (`mini_ork/integrations/telegram.py`) is the reference shape — mirror it.

**Deliverable.** `mini_ork/integrations/_ws.py` (a minimal WebSocket client),
`mini_ork/integrations/slack.py` (`SlackTransport`), registration beside Telegram in
`mini_ork/integrations/__init__.py`, and `tests/unit/test_integrations_slack.py` +
`tests/unit/test_integrations_ws.py`.

**Scope:**
- **`_ws.py` — stdlib only** (`socket`, `ssl`, `base64`, `hashlib`, `os`, `struct`): connect to a
  `wss://` URL, perform the RFC 6455 handshake and verify `Sec-WebSocket-Accept`, send masked
  text frames, receive text frames (reassembling continuations), answer ping with pong, and
  surface close. No extensions, no compression. The runtime dependency list stays
  `PyYAML` + `jsonschema`. Discord's Gateway will reuse this module, so keep it transport-free.
- **Simple tier — Socket Mode (no public URL).** `apps.connections.open` with the app-level
  token (`xapp-…`) over stdlib `urllib` → the `wss` URL → read envelopes over `_ws.py`; ack
  every envelope with `{"envelope_id": …}` before processing; for an `events_api` envelope whose
  event is a user `message` (ignore bot messages and every `subtype`), deliver its text to
  `thread_key("slack", channel, thread_ts or ts)` with origin `CHAT`. A redelivered envelope
  (same event id) is a no-op — reuse the thread store's `external_id` dedupe the way Telegram
  does. A bounded `poll_once`-style method (one connection, N envelopes or a timeout) is what
  tests drive.
- **Secure tier — Events API webhook.** `handle_webhook(headers, body)`: verify
  `X-Slack-Signature` = `v0=` HMAC-SHA256 of `v0:{X-Slack-Request-Timestamp}:{raw body}` with the
  signing secret, compared with `hmac.compare_digest`; reject a timestamp more than 300 s from
  now; answer `url_verification` with its `challenge`; then the same message handling as above.
  Share the HMAC shape `mini_ork/intake/webhooks.py` uses.
- **`send(thread_id, text)`** → `chat.postMessage` with the bot token (`xoxb-…`) into the
  Slack thread the thread id maps back to.
- **Secrets — fail closed, like Telegram's `_resolve_fail_closed`.** Three vault key names:
  `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`. A missing one raises an error
  naming the key, never an empty string; no token ever appears in `__repr__`, a log line or an
  exception message.

**Acceptance.** Offline — `urllib` and the socket monkeypatched, `_ws.py` exercised against
`socket.socketpair()`:
- `_ws.py`: handshake accepts the right `Sec-WebSocket-Accept` and refuses a wrong one; a masked
  frame round-trips; ping gets a pong; a fragmented message reassembles;
- Socket Mode: a first user message opens a thread and returns `launched`; a second message in
  the same Slack thread returns `steered` or `continued`, never a second concurrent run; a bot
  message and a `message_changed` subtype are ignored; every envelope is acked; a redelivered
  envelope is a no-op;
- Events API: a valid signature is accepted, a wrong one and a stale timestamp are refused,
  `url_verification` returns the challenge;
- a missing `SLACK_BOT_TOKEN` raises naming the key; no token in any raised message.

- max attempts: 2

---

## Scope Hint

- `mini_ork/integrations/_ws.py`
- `mini_ork/integrations/slack.py`
- `mini_ork/integrations/__init__.py`
- `tests/unit/test_integrations_ws.py`
- `tests/unit/test_integrations_slack.py`
- `mini_ork/integrations/telegram.py` — read-only reference: the shape to mirror
- `mini_ork/integrations/_base.py` — read-only reference: `thread_key`, `Transport`

## Verification commands
- `python3.11 -c "import mini_ork.integrations.slack, mini_ork.integrations._ws"`
- `python3.11 -m pytest -q tests/unit/test_integrations_ws.py tests/unit/test_integrations_slack.py tests/unit/test_integrations_telegram.py`

Run these from the run's working tree (the isolated worktree), so `mini_ork` resolves to the tree under test. A syntax check is not acceptance.

## Done When

- `${MINI_ORK_RUN_DIR}/panel-verdict.json` contains `"verdict": "pass"` with all verifiers passing.
- All verification commands pass in the isolated worktree.
