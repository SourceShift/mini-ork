# Chat intake

mini-ork can open and drive a thread from a message on a chat surface. An
inbound message is reduced to the same durable, multi-turn **thread** every
other entry point uses (`origin: chat`), and the thread state machine decides
what happens next: a first message **launches** a run, a message while a run is
live **steers** it, and a message after it finished **continues** the thread
with the transcript attached. Launching goes through
`mini_ork.web.control.launch_run`; steering through `steer_run`; neither is
reimplemented per transport.

**One-way today.** Results are not posted back to the chat: the run writes its
transcript into the thread, and you read it where every other thread is read:

```bash
mini-ork threads list --origin chat
mini-ork threads show <id>
```

Posting assistant turns back to Telegram/Slack/Discord is not wired yet.

## The config file

One file selects the transport and the tier: `$MINI_ORK_HOME/config/chat.yaml`
(or the path named by `$MINI_ORK_CHAT`). Start from
[`config/chat.example.yaml`](../../config/chat.example.yaml):

```yaml
transport: telegram        # telegram | slack | discord
tier: simple               # simple | secure
allow_channels: ["123456789"]
# recipe: code-fix         # optional run-recipe override
```

Those four keys are the whole vocabulary. Any other key is refused — in
particular a secret pasted into the file is an error, not a silently accepted
value. The file holds **names**, and the secrets live in the vault.

### The channel allowlist

`allow_channels` is **mandatory and default-deny**. Anyone who can message a bot
could otherwise start runs on the operator's machine, so a message from a
channel that is not listed is reported `ignored` *before* the thread state
machine runs: no thread row, no run.

- **Telegram**: a DM's chat id is the user id (positive); a group's is negative.
- **Slack**: a channel or DM id, `C…` or `D…`.
- **Discord**: a channel snowflake id.

The allowlist is channel-level, not per-user. A shared channel admits every
member who can post in it.

## Secrets

Secrets come only from the vault, resolved by key name (never from argv, never
from the config file, never from a log line). A missing key fails closed: chat
never falls back to an empty secret, and `threads serve` exits 2 before any
network call.

| transport | simple tier | secure tier |
|---|---|---|
| telegram | `TELEGRAM_BOT_TOKEN` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` |
| slack | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` |
| discord | `DISCORD_BOT_TOKEN` | `DISCORD_BOT_TOKEN`, `DISCORD_PUBLIC_KEY` |

Check where a key resolves from with `mini-ork vault which <KEY>`.

On a laptop the zero-setup tier is `$MINI_ORK_HOME/config/secrets.local.sh`
(mode `0600`), read by the vault's `file` backend. A shared or audited
deployment wants a `sops`, `command` or `openbao` backend in
`$MINI_ORK_HOME/config/vault.yaml`; a **secure**-tier route without one answers
`503` rather than accepting an unprotected secret.

## Simple tier: `mini-ork threads serve`

```bash
mini-ork threads serve                 # transport/tier from chat.yaml
mini-ork threads serve --transport T   # must match chat.yaml
mini-ork threads serve --once          # one tick, then exit (a probe)
```

The loop polls the transport's outbound API — no public URL, no server:

- **Telegram** — long poll (`getUpdates`). Do not set a webhook at the same
  time: a webhook makes `getUpdates` return nothing. Run
  `deleteWebhook` first if one was left configured.
- **Slack** — Socket Mode. In the app: turn Socket Mode on, create an
  app-level token with the `connections:write` scope (this is
  `SLACK_APP_TOKEN`), and subscribe the bot to the `message.channels` and
  `message.im` events.
- **Discord** — `--once` only. The Gateway loop is refused: each tick opens a
  new session (IDENTIFY) that cannot RESUME, so looping would spend the bot's
  daily session-start budget and drop messages between ticks. Use the secure
  tier, or `--once` as a single probe.

`--once` runs exactly one tick and exits; a failed tick exits 1, an idle one
(telegram long-poll timeout) exits 0. The long-running loop backs off
exponentially on errors (1 s doubling to 60 s) and stops cleanly on Ctrl-C.

`tier: secure` is not served by this loop. It is the web route below, under
`mini-ork serve`; `threads serve` points at it and exits 2.

## Secure tier: `POST /api/v1/chat/<transport>`

Run `mini-ork serve --host …` behind TLS and register the URL with the platform.
Every request is signature-verified; there is no unauthenticated path.

- **Telegram** — `setWebhook` pointed at the route, with `secret_token` set to
  your `TELEGRAM_WEBHOOK_SECRET`. The native
  `X-Telegram-Bot-Api-Secret-Token` header is checked, as is the house HMAC.
- **Slack** — Events API Request URL = the route; the signing secret
  (`SLACK_SIGNING_SECRET`) verifies `X-Slack-Signature` and the timestamp.
  Answer the URL-verification challenge when Slack sends it.
- **Discord** — Interactions Endpoint URL = the route;
  `DISCORD_PUBLIC_KEY` verifies the Ed25519 signature. Install the extra
  (`pip install 'mini-ork[integrations]'`) and register a slash command with one
  string option.

Status codes:

| code | meaning |
|---|---|
| `200` | accepted (including replayed / ignored deliveries) |
| `400` | bad JSON body |
| `401` | bad or missing signature |
| `404` | that transport is not enabled |
| `413` | body too large |
| `503` | protection gap, missing secret or bad config |

## Known limits

- No reply path: results are not posted back to the chat.
- No Discord Gateway loop (use the Interactions webhook or `--once`).
- The allowlist is channel-level, not per-user.
- The simple tier is one process per bot; the secure tier scales behind your
  web deployment.
