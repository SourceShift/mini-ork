# ECOSYSTEM §6 · K15a: make chat reachable — config, allowlist and `mini-ork threads serve` (simple tier)

## Goal

`mini_ork/integrations/` holds three chat transports (Telegram, Slack,
Discord). Each turns an inbound message into
`mini_ork.threads.service.deliver(...)`, which opens the `threads` row (origin
`chat`) and launches, steers or continues a run through
`mini_ork.web.control.launch_run` / `steer_run`; 92 unit tests pin this. Nothing
calls the transports — no CLI verb, no route, no config — so the feature is
unreachable.

This kickoff adds the shared glue and the **simple (laptop) tier**:
`mini-ork threads serve [--transport T] [--once]` looping on the transport's
`poll_once`, driven by one config file `$MINI_ORK_HOME/config/chat.yaml`
(example `config/chat.example.yaml`) that names the transport, the tier and a
**mandatory channel allowlist** (anyone who can message a bot could otherwise
start runs on the operator's machine). Secrets come only from the vault, by key
name. The secure tier (webhook routes) is the follow-up kickoff K15b and builds
on `build_transport` from this one.

Out of scope: posting run results back to the chat, a looping Discord Gateway,
per-user allowlists, the web routes, and any edit to a transport module.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/integrations/serve.py` (new)

Importing it must read no file, no vault and open no socket.

Module-level imports: `os`, `sys`, `time`, `dataclasses`, `pathlib.Path`,
`typing.Any`, `typing.Mapping`;
`from mini_ork.dispatch.secrets import SecretStoreError`;
`from mini_ork.dispatch.vault import vault_environment` (module-level name so
tests can monkeypatch `serve.vault_environment`);
`from mini_ork.integrations import discord, slack, telegram`;
`from mini_ork.integrations._base import PollResult`.
(Verify each imported name exists before using it; if `SecretStoreError` or
`vault_environment` live elsewhere, import them from where they are.)

Constants:
- `CONFIG_ENV = "MINI_ORK_CHAT"`, `TRANSPORTS = ("telegram", "slack", "discord")`,
  `TIERS = ("simple", "secure")`,
  `ALLOWED_KEYS = frozenset({"transport", "tier", "allow_channels", "recipe"})`.
- `TELEGRAM_BOT_TOKEN_KEY = "TELEGRAM_BOT_TOKEN"`,
  `TELEGRAM_WEBHOOK_SECRET_KEY = "TELEGRAM_WEBHOOK_SECRET"` — env-safe names; the
  transport's own `telegram.TOKEN_VAULT_KEY` is dotted, which neither
  `secrets.local.sh` nor a shell export can hold, so this module resolves these
  and passes the values in explicitly.
- `SECRET_KEYS[(transport, tier)]`:

  | transport | simple | secure |
  |---|---|---|
  | telegram | `TELEGRAM_BOT_TOKEN_KEY` | `TELEGRAM_BOT_TOKEN_KEY`, `TELEGRAM_WEBHOOK_SECRET_KEY` |
  | slack | `slack.BOT_TOKEN_VAULT_KEY`, `slack.APP_TOKEN_VAULT_KEY` | `slack.BOT_TOKEN_VAULT_KEY`, `slack.SIGNING_SECRET_VAULT_KEY` |
  | discord | `discord.BOT_TOKEN_VAULT_KEY` | `discord.BOT_TOKEN_VAULT_KEY`, `discord.PUBLIC_KEY_VAULT_KEY` |

- `SOCKET_IDLE_SECONDS = 600.0` (simple-tier `ws_timeout` for Slack/Discord);
  `SOCKET_FRAMES_PER_TICK = 10_000` (Slack `limit`; the first Socket Mode frame
  is `hello`, so the default `limit=1` never sees a message);
  `BACKOFF_START = 1.0`, `BACKOFF_CAP = 60.0`.

`class ChatConfigError(RuntimeError)` — messages name a path, key or setting,
never a value.

`@dataclass(frozen=True, slots=True) class ChatConfig`: `transport: str`,
`tier: str`, `allow_channels: frozenset[str]`, `recipe: str | None`, `path: Path`.

`config_path(home, environ=None) -> Path`: `Path(environ[CONFIG_ENV])` when set
and non-blank, else `Path(home) / "config" / "chat.yaml"`.

`load_chat_config(home, environ=None) -> ChatConfig | None`:
- File absent: `None` when `CONFIG_ENV` is unset, else
  `ChatConfigError(f"chat config {path} does not exist")`.
- `yaml.safe_load`; `OSError` / `yaml.YAMLError` → `ChatConfigError`; top level
  must be a mapping.
- A key outside `ALLOWED_KEYS` →
  `ChatConfigError(f"{path}: unknown key {key!r} — chat.yaml holds names, never secrets; secrets live in the vault")`
  (names the key only, never its value).
- `transport` in `TRANSPORTS`; `tier` in `TIERS`.
- `allow_channels`: non-empty list; each item `str(item).strip()`, blanks
  refused; missing/empty →
  `ChatConfigError(f"{path}: allow_channels is required and must name at least one chat/channel id — chat never accepts messages from everywhere")`.
- `recipe`: optional non-blank str, else `None`.

`resolve_secrets(config, environment=None) -> dict[str, str]`:
`env = environment if environment is not None else vault_environment()`
(`SecretStoreError` → `ChatConfigError`); resolve every key in
`SECRET_KEYS[(config.transport, config.tier)]`; any `None`/`""` →
`ChatConfigError(f"chat: no value for {', '.join(missing)} from {env.describe()} — add it to the vault (see `mini-ork vault which <KEY>`); chat never falls back to an empty secret")`.

**Gated transports** — a message from a channel outside `allow_channels` is
reported `ignored` before `service.deliver`, so no thread row and no run. Each
has `__init__(self, *, allow: frozenset[str], **kw)` storing `self._allow`, then
`super().__init__(**kw)`:
- `_GatedTelegram(telegram.TelegramTransport)` overrides
  `_ingest(self, update, *, home, db)`: `message = update.get("message") or update.get("edited_message")`;
  channel = `message["chat"]["id"]` when both are Mappings; if
  `str(channel) not in self._allow` →
  `PollResult(action="ignored", update_id=update.get("update_id"))` (cursor still
  advances); else `super()._ingest(...)`.
- `_GatedSlack(slack.SlackTransport)` overrides `_ingest(self, body, *, home, db)`:
  channel = `body["event"]["channel"]` when both are Mappings, else `None`; not
  allowed → `PollResult(action="ignored")`.
- `_GatedDiscord(discord.DiscordTransport)` overrides
  `_deliver(self, payload, *, channel, text, home, db)`: not allowed →
  `PollResult(action="ignored")`.
(Match the real signatures of `_ingest` / `_deliver` in each transport; if a
signature differs, mirror it exactly.)

`build_transport(config, *, environment=None, home=None, db=None)`: resolve the
vault once (`SecretStoreError` → `ChatConfigError`); `secrets = resolve_secrets(config, env)`;
construct the gated class with `environment=env`, `recipe=config.recipe`,
`home=home`, `db=db`, `allow=config.allow_channels`, plus:
- telegram: `token=secrets[TELEGRAM_BOT_TOKEN_KEY]`,
  `webhook_secret=` and `secret_token=` both `secrets.get(TELEGRAM_WEBHOOK_SECRET_KEY)`;
- slack: `bot_token=`, `app_token=secrets.get(APP)`,
  `signing_secret=secrets.get(SIGNING)`, and `ws_timeout=SOCKET_IDLE_SECONDS`
  when the tier is simple;
- discord: `bot_token=`, `public_key=secrets.get(PUBLIC)`, and
  `ws_timeout=SOCKET_IDLE_SECONDS` when simple.
Wrap `TelegramConfigError` / `SlackConfigError` / `DiscordConfigError` from
construction in `ChatConfigError(str(exc))`.

`serve(transport, *, home, db, once=False, sleep=time.sleep, out=None, err=None) -> int`
(`out`/`err` default to `sys.stdout`/`sys.stderr`):
- Discord without `once`: write to `err` "chat: the Discord Gateway tier opens a
  new session (IDENTIFY) per tick and cannot RESUME — looping it spends the
  bot's daily session-start budget and drops messages between ticks. Use tier:
  secure (the Interactions webhook), or --once for a single probe." and return 2
  without calling `poll_once`.
- Banner to `err`: `chat: serving {name} (simple tier) into {home} — Ctrl-C to stop`.
- Each tick: telegram `poll_once(offset=cursor, home=home, db=db)`; slack
  `poll_once(home=home, db=db, limit=SOCKET_FRAMES_PER_TICK)`; else
  `poll_once(home=home, db=db)`.
- Success: if `result.next_offset is not None`, `cursor = result.next_offset`;
  for each `r` in `result.batch or (result,)` with `r.action != "ignored"` and
  `r.delivery` set, write `f"{r.action}: thread {r.delivery.thread_id} run {r.delivery.run_id or '-'}\n"`
  to `out`; reset backoff; if `once`, return 0.
- `TimeoutError`: no backoff, nothing on `err`; if `once`, return 0.
- `KeyboardInterrupt`: write `chat: stopped` to `err`, return 0.
- Any other `Exception`: write `f"chat: {name} tick failed: {type(exc).__name__}: {exc}\n"`
  to `err`; if `once`, return 1; else `sleep(backoff)`,
  `backoff = min(backoff * 2, BACKOFF_CAP)`.

`__all__` lists the public names.

### 2. `mini_ork/threads/cli.py`: the `serve` verb

- Add `mini-ork threads serve [--transport T] [--once]` to `_USAGE`'s usage list
  and the verb line
  `serve      run the chat simple tier from config/chat.yaml (telegram long-poll, slack socket mode); see docs/operator/chat.md`.
- `main`: `if verb == "serve": return _cmd_serve(rest)`.
- `_cmd_serve(argv)`: argparse prog `mini-ork threads serve`, `--transport`
  (choices telegram/slack/discord, default None), `--once` (store_true), and the
  module's existing common options helper. Then, in order:
  1. `home, db = <the module's existing home/db resolver>(args)`.
  2. lazy `from mini_ork.integrations import serve as chat`.
  3. `config = chat.load_chat_config(home)`; `ChatConfigError` → stderr
     `threads serve: {exc}`, return 2.
  4. `config is None` → stderr
     `threads serve: no chat config at {chat.config_path(home)} — copy config/chat.example.yaml there and set transport, tier and allow_channels`,
     return 2.
  5. `args.transport` set and different from `config.transport` → stderr line
     naming both, return 2.
  6. `config.tier == "secure"` → stderr
     `threads serve: {config.path} selects tier: secure — that tier is the web route POST /api/v1/chat/{config.transport} under \`mini-ork serve\`, not this loop`,
     return 2.
  7. `transport = chat.build_transport(config, home=home, db=db)`;
     `ChatConfigError` → stderr, return 2 (no network touched yet).
  8. `return chat.serve(transport, home=home, db=db, once=args.once)`.
- Change no other verb.

### 3. `config/chat.example.yaml` (new)

`transport: telegram`, `tier: simple`, `allow_channels: ["123456789"]`,
`# recipe: code-fix`. Comments list the vault key names per transport and tier
(from `SECRET_KEYS`), say "copy to $MINI_ORK_HOME/config/chat.yaml (or point
$MINI_ORK_CHAT at it)", say the file holds names only and a secret in it is
refused, and name both entry points (`mini-ork threads serve`; the secure-tier
route `POST /api/v1/chat/<transport>`).

### 4. `docs/operator/chat.md` (new)

What it does (message → thread `origin: chat` → launch/steer/continue a run;
**one-way today**: results are not posted back — watch with
`mini-ork threads list --origin chat` / `threads show <id>`); the config file and
the allowlist (Telegram DM chat id = user id, group ids negative; Slack
`C…`/`D…`; Discord channel snowflake); secrets (the key table,
`mini-ork vault which`; laptop: `secrets.local.sh` 0600; the secure tier needs a
sops/command/openbao backend or the route answers 503); simple tier per
transport (Telegram: no webhook set at the same time — `deleteWebhook` first;
Slack: Socket Mode on, an app-level token with `connections:write`, bot events
`message.channels` and `message.im`; Discord: `--once` probe only, and why);
secure tier per transport (`mini-ork serve --host …` behind TLS; Telegram
`setWebhook` with `secret_token` = `TELEGRAM_WEBHOOK_SECRET`; Slack Events API
Request URL + signing secret; Discord Interactions Endpoint URL,
`pip install 'mini-ork[integrations]'`, register a slash command with one string
option); status codes (404 not enabled, 413 too large, 503 protection
gap/missing secret/bad config, 401 bad signature, 400 bad JSON, 200 otherwise);
known limits (no reply path, no Discord Gateway loop, channel-level allowlist).

### 5. `docs/epics/EPIC-work-intake-thread-communication.md`

Line 174 only: replace "the Telegram transport landed in `bb258b91`; Slack and
Discord are not built." with "Telegram, Slack and Discord transports are built
(both tiers each) and reachable via `mini-ork threads serve` (simple) and
`POST /api/v1/chat/<transport>` (secure), selected by `config/chat.yaml` — see
`docs/operator/chat.md`. Posting results back to the chat is not wired yet."

## Files in scope (touch ONLY these)

- `mini_ork/integrations/serve.py` (new)
- `mini_ork/threads/cli.py`
- `config/chat.example.yaml` (new)
- `docs/operator/chat.md` (new)
- `docs/epics/EPIC-work-intake-thread-communication.md` (line 174 only)
- `tests/unit/test_chat_serve.py` (new)

Do NOT modify any other file under `mini_ork/integrations/`, `mini_ork/web/`,
or `pyproject.toml`. The tests in `tests/unit/test_integrations_*.py` must pass
unchanged.

## Tests (`tests/unit/test_chat_serve.py`, hermetic: no socket, no platform API, no process)

Fixtures: autouse `delenv` of `MINI_ORK_DB`, `MINI_ORK_CHAT`, `MINI_ORK_HOME`;
replace `mini_ork.web.control.launch_run` and `steer_run` with recorders (shape:
`tests/unit/test_web_intake_route.py`);
`monkeypatch.setattr(serve, "vault_environment", lambda: _Env({...}))` where
`_Env` has `resolve(key)` (None when missing) and `describe()`. `home`: a
migrated `state.db` under `tmp_path` via `mig.init_db`. `write_config(home, **doc)`
writes `home/config/chat.yaml`.

1. No file → `load_chat_config(home) is None`; `MINI_ORK_CHAT` naming a missing
   file raises.
2. A valid file parses; integer channel ids become str.
3. `ChatConfigError` for: unknown transport; unknown tier; missing/empty
   `allow_channels`; unknown key `bot_token: s3cr3t` (message names `bot_token`,
   does not contain `s3cr3t`).
4. `build_transport` slack/secure with no `SLACK_SIGNING_SECRET` (or whatever
   `slack.SIGNING_SECRET_VAULT_KEY` is) → `ChatConfigError` naming that key and
   containing none of the supplied values.
5. telegram/simple passes `TELEGRAM_BOT_TOKEN` through (`.token` equals it;
   `repr()` does not contain it).
6. Telegram allowlist through a real poll: monkeypatch
   `telegram.urllib.request.urlopen` so `getUpdates` returns update 5 from an
   allowed chat and update 6 from a denied chat → exactly one launch,
   `store.get_thread("telegram-<denied>", db=db) is None`, `next_offset == 7`
   (use the transport's real thread-id/offset helpers; adapt names to the code).
7. `_GatedDiscord._deliver` with a denied channel → `ignored`, no thread row.
8. Stub transport (`name` + scripted `poll_once(**kw)`), `once=True` →
   `poll_once` called once, one line per non-ignored batch result, return 0.
9. Telegram-named stub returning `update_id` 5, then 9, then raising
   `KeyboardInterrupt` → recorded offsets `[None, 6, 10]`, return 0.
10. Backoff: `OSError`, `OSError`, `TimeoutError`, `KeyboardInterrupt` → sleeps
    `[1.0, 2.0]`; with `once=True` a single `OSError` returns 1 without sleeping.
11. Discord-named stub, `once=False` → return 2, `poll_once` never called.
12. CLI `mini_ork.threads.cli.main(["serve", "--home", str(home)])` with no
    chat.yaml → exit 2, stderr names the path and `config/chat.example.yaml`.
13. Exit 2 for: mismatched `--transport`; `tier: secure`; a missing secret
    (stderr names the key; a recorder on `serve.serve` shows it was never called).
14. Happy path: monkeypatch `serve.build_transport` to return a stub →
    `serve --once` exits 0, the stub got `home` = the resolved home and `db` =
    `home/state.db`.

## Verification commands

- `env -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_chat_serve.py tests/unit/test_integrations_telegram.py tests/unit/test_integrations_slack.py tests/unit/test_integrations_discord.py tests/unit/test_integrations_ws.py tests/unit/test_threads_cli.py tests/unit/test_threads_service.py`
- `bin/mini-ork threads help | grep -q "threads serve"`
- `ruff check --select F,E9 mini_ork/integrations/serve.py mini_ork/threads/cli.py`

## Self-application measurement

```measure
env -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio tests/unit/test_chat_serve.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k15a-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k15a-junit.xml`

## Success criteria

- Every verification command passes (drop any listed test file that does not
  exist; do not create it); every pre-existing test unchanged and green.
- No transport module changes.
- No secret value appears in any argv, config file, stdout or stderr line from
  the new code.
- A missing secret stops `threads serve` before any network call (exit 2).
