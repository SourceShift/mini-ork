# EPIC — Work intake, communication and file handling: the Thread as one primitive

> **Update 2026-10-13:** the delivery-order table in §3 is current — Linear, Jira, write-back and
> the ticket board are shipped; the Stage 2 Rust renderer and Slack/Discord remain.
>
> **Status: Stages 0 and 1 are SHIPPED (10 commits, `90695bb4` @ 2026-10-11). Stages 2 and 3 are
> planned and unbuilt.** This epic exists because the feature landed as a sequence of commits
> across three subsystems (`pm/`, `threads/`, `intake/`) plus an IDE surface, and nothing in the
> repo collected them into one place.
>
> Plan of record: `~/.claude/plans/fluffy-squishing-locket.md`. Distilled from that plan, the
> shipped code, and its module docstrings — the docstrings are the design rationale, and this
> epic points at them rather than restating them.

**Goal.** Let work *arrive* from the tools a team already uses (Linear, Jira, GitHub), let a
person *converse* about it (Slack, Telegram, Discord), and let both *attach files* — built on one
shared primitive rather than three incompatible notions of "a conversation".

**The thesis.** All three capabilities are an external surface that opens a unit of work and can
carry follow-up messages about it. Build that once — a **Thread** — and intake, messaging and
attachments become adapters over it. The alternative, three separate builds, produces three
notions of "conversation" that cannot share a transcript.

---

## 1. What is already shipped

### 1.1 `mini_ork/pm/` — one model for every source of work

Commit **`789a1376`** *feat(pm): one model for every source of work, with mini-ork's own epics
first*.

A provider-neutral vocabulary so a board can show mini-ork's own epics next to a Linear issue and
a GitHub issue without any of them lying. Nothing in production read it at the time — it is the
foundation the later stages load.

| Module | What it is |
|---|---|
| `pm/model.py` | `Issue`, the **closed seven-value** `IssueState`, `Priority`, `LinkKind`. Two deliberate side-picks: a state that maps to nothing lands in `BACKLOG` and the source's own word survives in `native_state` (normalizing is never lossy); `Priority` counts **up**, with the Linear inversion written once in `Priority.from_linear` |
| `pm/provider.py` | The `Provider` protocol **plus `Capabilities`** — a source declares what it cannot do (the `local` adapter declares `transition=False` because `trg_epics_no_revert_done` refuses it), so the surface never draws a control that would be refused |
| `pm/registry.py` | Source id → adapter, registering only *configured* sources; a broken YAML degrades to local-only instead of taking down `import mini_ork.pm` |
| `pm/store.py` | The **mirror** (`pm_issues`/`pm_links`/`pm_sync`): an index, not a second truth. Partial rows on purpose — `IssueRow` ≠ `Issue`, so a caller cannot mistake the mirror for complete |
| `pm/config.py` | `config/pm.yaml` — source config; secrets are **key names only** (`token_env`) |
| `pm/http.py` | The one HTTP call, over **stdlib `urllib.request`** (no `requests`/`httpx`; the runtime dep list stays `PyYAML` + `jsonschema`). A `ProviderError` names the source and operation and **scrubs every credential value it sent** out of the error text |
| `pm/providers/local.py` | mini-ork's own epics — no credential, no network |
| `pm/providers/github.py` | Commit **`19046260`** *feat(pm): read GitHub issues — the first source that needs a credential*. Read-only (create/update/comment not implemented), so the board shows issues without offering a write it would refuse |

Migration: **`db/migrations/0069_pm_mirror.sql`**. Config: **`config/pm.yaml`** (documents the
GitHub source and commented-out `linear:`/`jira:` stanzas).

### 1.2 `mini_ork/threads/` — the Thread primitive

A durable, multi-turn unit of work. This is what the old agent-server sidecar could not express:
it held one DAG and answered every follow-up with `409 "conversation run already finished — start
a new conversation"`.

Commits, in order:

| Commit | Delivers |
|---|---|
| `76051a32` | *add the thread spine* — migration `0070_threads.sql`, `model.py`, `store.py` |
| `7061d757` | *compose the kickoff a continuation run receives* — `kickoff.py`, pure |
| `b19cf53d` | *deliver a message into a thread — the 409's replacement* — `service.py` |
| `214895ce` | *drive threads from the CLI with no server* — `threads/cli.py` |
| `1c575dc0` | *retire the agent-server 409* — a follow-up continues the thread (`agent_server.py`) |

**The state machine *is* the feature.** `service.deliver(thread_id, text, …)` returns exactly one
of four outcomes:

```
launched    nothing has run yet        → run attempt 1, run_id == thread_id
steered     a run is live              → inject into the run, never spawn a second
continued   the last run is terminal   → run attempt N, same thread, kickoff carries history
duplicate   this external message is known → do nothing
```

Two properties are load-bearing:

- **`steered` is preserved verbatim** — `steer_run` rides the next `context_assemble` pack, so a
  conversation the canvas is mid-way through behaves exactly as before.
- **`continued` is the 409's replacement** — a follow-up to a finished run is a *new run in the
  same thread*, with the transcript and prior verdict carried in the kickoff text, because a run
  is a fresh detached process and cannot inherit context any other way.

Identity is deliberately translation-free: `conversation_id == thread_id`; attempt 1 uses
`run_id == thread_id`; attempt N uses `f"{thread_id}-t{turn}"`.

Locking: turn allocation takes `BEGIN IMMEDIATE` before reading `last_turn`, so a Slack reply and
a websocket tick arriving together cannot pick the same turn.

### 1.3 `mini_ork/intake/` — a ticket becomes work

Commits **`e29fa1b3`** (tier A, polling) and **`5dd61439`** (tier B, signed webhook).

| Module | What it is |
|---|---|
| `intake/trigger.py` | The rules — **all opt-in** (`trigger_label`, `trigger_assignee`, `trigger_states`, `trigger_recipe`), ANDed. With none set `matches_trigger` returns `(False, "no trigger configured")`. A half-configured source starts **nothing** rather than the whole backlog |
| `intake/poll.py` | One pass = sync → filter the mirror → dispatch. Filtering reads only `state`/`labels`/`assignee`, all on a mirror row, so a hundred-ticket board costs **zero** extra requests |
| `intake/dispatch.py` | `dispatch_issue` — the *one* place a ticket becomes work, shared by both tiers so they cannot drift. **"Never two at once, and the second one continues the same thread."** |
| `intake/webhooks.py` | Tier B cryptography + decision — **no fastapi import**, so the HMAC is testable without a server |
| `cli/intake_cmd.py` | `mini-ork intake {once,poll,status}` — tier A entry point |
| `web/routes/intake.py` | `POST /api/v1/intake/{source}` — the HTTP shell; owns status codes only |

**Dedup is three questions with three mechanisms, not one:** *has a run ever worked this ticket?*
→ `pm_links kind='run'`; *is one working it right now?* → the `pm_links` → `thread_runs` live-index
join; *have I handled this exact event?* → `intake_events` keyed `(provider, delivery_id)`.

### 1.4 IDE surface

Commit **`90695bb4`** *feat(ide): a thread page, and an intake tab on the orchestrator page*.

- `mini_ork/ide_pages/thread.py` — the thread index and one thread: runs, markdown transcript
  (`### {who} · turn {n}`), and a **`composer`** that runs the same `mini-ork threads send <id>
  --text` the terminal does. The page adds no way to start work the CLI does not already have.
- `mini_ork/ide_pages/orch.py` — an `intake` tab appended to the existing cold orchestrator page:
  source table, recent arrivals ledger, and Preview/Poll buttons (Poll is `confirm`-gated —
  starting runs is not something a stray click should do).
- Both use **only existing section builders**, so `spec.py` needed no edit.

### 1.5 Test surface

**263 tests, 57 s**, all green:

```
tests/unit/test_pm_{model,registry,store,config,local,github,http}.py
tests/unit/test_threads_{store,service,cli}.py  tests/unit/test_thread_kickoff.py
tests/unit/test_intake_{trigger,poll,dispatch,webhooks}.py
tests/unit/test_web_intake_route.py             tests/unit/test_ide_pages_thread.py
```

Plus the IDE dispatch sweep (`test_ide_pages_actions.py`) walks every `cli` action on every page —
`"orch": ("intake",)` is pinned there, so a page that emits an action its parser cannot resolve
fails loud rather than silently.

### 1.6 The two-tier rule, as shipped

Per `CLAUDE.md`, both tiers are part of done, one config file chooses, and a missing secret
**fails closed**.

| | Simple (laptop, no server) | Secure (cloud-scalable) |
|---|---|---|
| Intake trigger | `mini-ork intake poll --interval 300` — outbound HTTPS only | `POST /api/v1/intake/{source}`, `--host 0.0.0.0` + TLS in front + HMAC |
| Driving a thread | `mini-ork threads send <id> --text …` (`launch_run` is a `Popen`) | `POST /api/conversations/{id}/events` |
| Secrets | `env` / `$MINI_ORK_HOME/config/secrets.local.sh` | `sops` / `command` / OpenBao |
| Replay | `payload_hash` folds a re-poll | `UNIQUE(provider, delivery_id)` + `payload_hash` |
| Failure mode | missing token ⇒ hard error naming the key | webhook route refuses **503** while the vault reports a protection gap |

---

## 2. What is planned and NOT yet built

### 2.1 Stage 2 — file handling

Preview in-app, agent writes.

- **`mini_ork/documents/`** (landed; the IDE section and attachments below are not) — read-only extraction with **zero new required
  deps**: XLSX/CSV → grid (`pandas`+`pyarrow`, already installed); PPTX/DOCX → text/structure via
  stdlib `zipfile` + `xml.etree` (both are ZIP+XML); PDF → page rasterisation, host binary first
  (`qlmanage`/`sips`, `pdftoppm`) with pinned `pymupdf` behind an optional extra. **Fail closed
  and loudly when neither is available — never a blank pane.**
- **IDE:** a new `document` section type — one arm in `render_section` plus `ui/src/documents.rs`.
  Split 2026-10-13: the engine half (attachments in, the `document` section payload) is
  `wit-2-attachment-section`; the Rust renderer is a follow-up in the `mini-ork-ui` repo
  (commit there, push its main, bump the `ui` pin here and in zed-mini-ork) because that
  crate compiles only inside the zed-mini-ork workspace and a run against this repo cannot
  verify it.
- **Attachments:** today `agent_server.py` explicitly drops non-text message parts. They become
  `thread_messages` rows of a file role referencing the stored path; the IDE needs a file picker.
- **"Edit"** = a composer beside the preview; the agent edits with `python-pptx`/`openpyxl`/
  `python-docx` **in the run sandbox** — agent-side tools, not engine dependencies.

### 2.2 Stage 3 — communication

Transports over the same Thread. Package **`mini_ork/integrations/`** — Telegram, Slack and Discord transports are built (both tiers each) and reachable via `mini-ork threads serve` (simple) and `POST /api/v1/chat/<transport>` (secure), selected by `config/chat.yaml` — see `docs/operator/chat.md`. Posting results back to the chat is not wired yet.

- **Simple tier (no public URL):** Telegram long-poll (`getUpdates`), Slack Socket Mode
  (outbound websocket), Discord Gateway. Keeps the runtime dep list at two.
- **Secure tier:** signed inbound webhook per platform, HMAC-verified.
- An inbound message resolves to a Thread by `(transport, channel, thread_ts)` and runs **the
  Stage 0 state machine** — "follow up in the same thread" is existing behaviour, not new logic.
  Outbound updates ride `service.observe`.

### 2.3 Provider breadth and write-back

- **`pm/providers/linear.py`** — absent; `Priority.from_linear` already exists and the `linear:`
  stanza is already commented into `pm.yaml`. GraphQL, one header, cursor pagination.
- **`pm/providers/jira.py`** — absent; heaviest auth (site + email + token) and ADF → markdown is
  the real work.
- **Write-back** (comment/transition on the ticket), gated by **capability AND** an explicit
  `write_back: true`. This is where `Capabilities` pays: an adapter that cannot comment is never
  asked.

### 2.4 Explicitly out of scope for Stages 0–1

No `scheduler.py` ingest or post-verdict hook (intake uses its own process); no `run_events`
writer change; no new `spec.py` section type; no `serve.py` TLS or background poller; and **no
unification of the ACP registry** (`acp/agent.py` `_thread_sessions` is a fourth, in-memory
notion of "thread" that should eventually point at `threads`, but it is a separate slice).

---

## 3. Delivery order

Each step lands and is verified before the next. Stages 0–1 are done; the numbering is the
original plan's.

| # | Step | State |
|---|---|---|
| 1 | `0070_threads.sql` + `threads/{model,store}.py` + DDL-agreement test | ✅ `76051a32` |
| 2 | `threads/kickoff.py` (pure) | ✅ `7061d757` |
| 3 | `threads/service.py` — the 409's replacement, as a function nobody calls | ✅ `b19cf53d` |
| 4 | `threads/cli.py` + `mini-ork threads {list,show,send,continue}` | ✅ `214895ce` |
| 5 | Wire `agent_server.py` (dual-write, status, events, delete the 409) | ✅ `1c575dc0` |
| 6 | `pm/http.py` + `pm/providers/github.py` + registration | ✅ `19046260` |
| 7 | `intake/{trigger,poll}.py` + `cli/intake_cmd.py` (tier A) | ✅ `e29fa1b3` |
| 8 | `web/routes/intake.py` + `intake/{webhooks,dispatch}.py` (tier B) | ✅ `5dd61439` |
| 9 | IDE `thread` page + `intake` tab on the cold `orch` page | ✅ `90695bb4` |
| 10 | **Stage 2** — `mini_ork/documents/` + `document` section + attachments | 🟡 engine done: documents `4598b66b`, attachments + `document` section payload `f4d5bb75`; Rust renderer in mini-ork-ui pending |
| 11 | **Stage 3** — `mini_ork/integrations/` transports | 🟡 Telegram landed `bb258b91`; Slack/Discord planned |
| 12 | Linear, then Jira adapters | ✅ Linear `4379ff03`, Jira `976c9e41`; tickets on the Board's Tasks tab `d28c2b0f` |
| 13 | Write-back behind capability + `write_back: true` | ✅ `1be093e8` (`mini_ork/pm/writeback.py`; no caller yet) |

---

## 4. Verification

**Shipped surface (must stay green):**

```bash
.venv/bin/python -m pytest -q tests/unit/test_pm_*.py tests/unit/test_threads_*.py \
  tests/unit/test_thread_kickoff.py tests/unit/test_intake_*.py \
  tests/unit/test_web_intake_route.py tests/unit/test_ide_pages_thread.py
# → 263 passed
```

**Keystones that pin the properties, not just the code:**

- `test_threads_service.py` — terminal → `action="continued"`, `attempt == 2`, same `thread_id`, a
  **different** `run_id`, and the new kickoff contains the prior turn's text *and* the prior run's
  verdict. A run live in `task_runs` but not yet in `thread_runs` is treated as terminal (fail
  safe) — never two live runs.
- `test_intake_trigger.py` — "no trigger configured" returns `(False, …)` so a misconfigured
  source cannot drain a backlog.
- `test_intake_webhooks.py` — `compare_digest` used; a missing secret raises; the same
  `delivery_id` twice is ignored, not re-dispatched; a 401 **does not leak the key**.
- `test_pm_http.py` — the credential is scrubbed from every raised message.
- `test_ide_pages_actions.py` — every `cli` action on every page resolves to a parser that accepts
  it, including the `orch` intake tab.

**End-to-end, simple tier:** with a real PAT in `secrets.local.sh`, run `mini-ork intake once
--dry-run`, then for real; confirm one Thread, one run, one `pm_links` run edge — and that a
second `once` starts nothing new.

**The reboot trap:** the live `uvicorn` on `:7090` (PID 55861, started Oct 10, **no `--reload`**)
serves pre-change Python, so engine changes are invisible in the running IDE until it is
restarted. This is an operator action, not an agent one.

---

## 5. Design invariants (do not break these)

1. **One state machine.** Every surface — poll, webhook, Slack, the IDE composer, the CLI — ends
   in `threads.service.deliver`. Two tiers must never grow two notions of "handled".
2. **Fail closed.** A missing secret is a clear error naming the key; an empty string is **never**
   read as "no credential needed". The webhook route refuses 503 on a vault protection gap.
3. **Opt-in triggers.** An unconfigured source is inert, not greedy. The safe default must hold
   even when someone forgets `--dry-run`.
4. **No new runtime dependencies.** Stdlib `urllib.request` for HTTP; stdlib `zipfile`+`xml.etree`
   for OOXML. The dep list stays `PyYAML` + `jsonschema`; `pymupdf`/`python-pptx` are optional
   extras or agent-side tools.
5. **Dual-sourced DDL.** Every new table's schema exists both as a migration and as `ensure_schema`
   in its `store.py`, with a test asserting the two texts agree — so a `state.db` that never ran a
   migration still works.
6. **Never lose the source's word.** `native_state` beside the normalized state; `IssueRow` ≠
   `Issue`.
7. **Capability, not `hasattr`.** A source declares what it cannot do; the UI never draws a
   control that would be refused.
