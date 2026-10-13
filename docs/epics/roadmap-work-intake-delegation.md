# Roadmap — delegate the unbuilt half of the work-intake epic

> Companion to `docs/epics/EPIC-work-intake-thread-communication.md`. That epic records what
> shipped (ten commits, `90695bb4` @ 2026-10-11); this roadmap is the five items it lists as
> **unbuilt**, each written as one deliverable so it can be dispatched without a hollow plan.
>
> Ingested with `bin/mini-ork epics ingest`, dispatched with `bin/mini-ork scheduler --epic …`.
> Every epic carries its own acceptance criteria and names the files in scope in backticks, so
> `epics split` synthesizes a verifier that points at real paths.
>
> **Every epic below must ship both tiers** (`CLAUDE.md`): the no-server simple tier and the
> secure, cloud-scalable tier, selected by one config file, failing closed on a missing secret.

**Status (2026-10-13).** The first scheduler pass (2026-10-12) delivered none of these cleanly:
every planner dispatch failed (`llm_dispatch FAIL … rc=4/65`) and the one child that passed
(Linear) wrote into the engine checkout, so its publisher abstained. The work was salvaged by hand:

| id | state |
|---|---|
| wit-2-documents | landed — package + tests salvaged from the escalated run |
| wit-2-attachment-section | not built (depends on wit-2-documents) |
| wit-3-integrations | Telegram transport landed `bb258b91`; Slack/Discord not built |
| wit-4-linear | landed `4379ff03` |
| wit-4-jira | not built — the run died at the planner |
| wit-5-writeback | not built (depends on wit-4-linear, wit-4-jira) |
| board-shows-imported-tickets | queued 2026-10-13 — see the last section |

---

## Stage 2 — document extraction (id: wit-2-documents)

Build the read-only extraction layer the file-handling stage is blocked on, as a new package
`mini_ork/documents/`. Preview in-app; the agent does the writing.

**Deliverable.** `mini_ork/documents/` with one entry point that returns a preview model for each
format, plus `tests/unit/test_documents_extract.py`.

**Scope and approach, per format:**
- XLSX/CSV → a grid, via `pandas` + `pyarrow` (already installed — add no dependency).
- PPTX/DOCX → text and structure via stdlib `zipfile` + `xml.etree` (both are ZIP+XML; a faithful
  read-only preview needs nothing more).
- PDF → page rasterisation, host binary first (`qlmanage`/`sips` on macOS, `pdftoppm` if present),
  with pinned `pymupdf` behind an optional extra as the predictable tier.

**Hard requirements.**
- **Fail closed and loudly.** When no rasteriser is available the call must raise a clear error
  naming what to install — **never a blank pane and never a silent empty result**.
- No new *required* runtime dependency: the list stays `PyYAML` + `jsonschema`.
- Nothing in this package writes; extraction is read-only.

**Acceptance.** `pytest tests/unit/test_documents_extract.py` is green and covers: a CSV and an
XLSX both yield a grid; a minimal DOCX and PPTX yield their text; a PDF with no rasteriser
available raises the documented error rather than returning empty; a missing optional dependency
degrades with a clear message.

- max attempts: 2

---

## Stage 2 — the `document` IDE section and attachments (id: wit-2-attachment-section)

depends on: wit-2-documents

Wire the extraction layer into the IDE and into threads.

**Deliverable.** A new `document` section type drawn by the IDE, and attachments carried on a
thread.

**Scope:**
- **IDE:** one new arm in `render_section` (`mini-ork/ui/src/page.rs`) plus a new
  `mini-ork/ui/src/documents.rs`. This is the established extension point — the same shape that
  added `files`, `findings` and `story` — a small edit, not a rewrite. Rust edits go in the
  **engine** checkout (`mini-ork/ui/src/*.rs`), which `script/mini-ork-sync-ui` copies into
  `crates/mini_ork_ui/`; do not edit the Zed fork.
- **Attachments:** `agent_server.py` today explicitly drops non-text message parts. Attachments
  become `thread_messages` rows of a file role referencing the stored path.
- **Picker:** the IDE needs a **file** picker (not folder) — `projects.rs` is the
  `prompt_for_paths` pattern to copy, with `files: true`.
- **Edit** stays a composer beside the preview: the user asks in the conversation and the agent
  edits with `python-pptx`/`openpyxl`/`python-docx` **in the run sandbox** — agent-side tools, not
  engine dependencies.

**Acceptance.** The page renders the new section for a real file and the unknown-section
placeholder is gone; a `thread_messages` row of file role round-trips through
`mini_ork/threads/store.py`; `pytest tests/unit/test_ide_pages_thread.py` stays green and a new
test covers the section; `./script/clippy` is clean for the crate.

- max attempts: 2

---

## Stage 3 — the communication transports (id: wit-3-integrations)

New package `mini_ork/integrations/`. Transports over the **same Thread** — an inbound message
resolves to a thread and runs the existing `threads.service.deliver` state machine, so "follow up
in the same thread" is existing behaviour rather than new logic.

**Deliverable.** `mini_ork/integrations/` with the first transport implemented end to end, plus
`tests/unit/test_integrations_telegram.py`.

**Scope:**
- **Simple tier (no public URL — this is what makes it real):** Telegram long-poll via
  `getUpdates`. Slack Socket Mode and Discord Gateway follow the same seam.
- **Secure tier:** a signed inbound webhook per platform, HMAC-verified, sharing the verification
  shape `mini_ork/intake/webhooks.py` already uses.
- Outbound updates ride `service.observe`.
- Keep the runtime dependency list at two: the websocket/poll clients are plain work over stdlib
  `urllib`, or the transport is gated behind an optional extra.

**Acceptance.** `pytest tests/unit/test_integrations_telegram.py` is green with the network
monkeypatched: a first inbound message opens a thread and returns `launched`; a second message on
the same `(transport, channel, thread_ts)` returns `steered` or `continued` — never a second
concurrent run; a redelivered update id is a no-op. A missing bot token raises a clear error
naming the vault key, never an empty string.

- max attempts: 2

---

## Linear adapter (id: wit-4-linear)

`mini_ork/pm/providers/linear.py` behind the existing `Provider` protocol.

**Deliverable.** A registered `linear` source that reads issues into the mirror, plus
`tests/unit/test_pm_linear.py`.

**Scope:**
- GraphQL over the shared stdlib helper `mini_ork/pm/http.py` — do not add `requests`/`httpx`.
- One `Authorization` header; cursor pagination maps onto `Query.cursor`/`Page.cursor`.
- `Priority.from_linear` **already exists and is tested** (`mini_ork/pm/model.py`) — Linear
  numbers urgency downward (1 urgent, 4 low) while mini-ork counts up; use the existing
  inversion, do not re-derive it.
- An unmapped state lands in `BACKLOG` with the source's own word preserved in `native_state` —
  normalizing must never be lossy.
- Registration only when configured; a broken config must degrade to local-only rather than
  taking down `import mini_ork.pm`.
- `_token()` raises `ProviderError` naming the key when it resolves to nothing — **never returns
  `""` as "no credential needed"**, and the credential never appears in a raised message.

**Acceptance.** `pytest tests/unit/test_pm_linear.py` is green with `pm.http.request_json`
monkeypatched: cursor passthrough; priority inversion correct at both ends; an unmapped state
becomes `BACKLOG` with `native_state` kept; `link()` raises `Unsupported`; a 401 does not leak the
key. The commented `linear:` stanza in `config/pm.yaml` is updated to a real, documented example.

- max attempts: 2

---

## Jira adapter (id: wit-4-jira)

`mini_ork/pm/providers/jira.py` behind the same protocol.

**Deliverable.** A registered `jira` source, plus `tests/unit/test_pm_jira.py`.

**Scope:**
- Heaviest auth of the three (instance URL + email + token) — the token and email are **vault key
  names only** in `config/pm.yaml`, never values.
- The real work is **ADF → markdown** for the description and comments; keep that conversion in
  one place and unit-test it against a captured ADF fixture.
- Same rules as Linear: stdlib HTTP only, `native_state` preserved, fail closed on a missing
  secret, credential scrubbed from every raised message.

**Acceptance.** `pytest tests/unit/test_pm_jira.py` is green offline: an ADF fixture converts to
the expected markdown (headings, lists, code blocks, links); an unmapped state becomes `BACKLOG`
with `native_state` kept; a 401 does not leak the token; a missing `email_env` key raises naming
the key. The commented `jira:` stanza in `config/pm.yaml` becomes a worked example.

- max attempts: 2

---

## Ticket write-back, capability-gated (id: wit-5-writeback)

Write back to the source: comment on the ticket and transition it.

depends on: wit-4-linear, wit-4-jira

**Deliverable.** Write operations on the adapters that can support them, gated so nothing is
attempted where it cannot succeed.

**Scope:**
- Gate on **capability AND** an explicit `write_back: true` in the source's `config/pm.yaml`
  stanza. This is what `Capabilities` exists for: an adapter that cannot comment declares it, and
  the surface never offers the control. A `hasattr` check is not enough — a method can exist and
  still be a no-op.
- Writes take an explicit `Draft`/`Patch`; there is no `update(uid, **kwargs)`, because "field
  absent" and "field set to empty" must stay distinguishable.
- The `local` adapter must keep `transition=False` — the DB trigger `trg_epics_no_revert_done`
  refuses a done epic going back, so a board that let you drag a card out of Done would be lying.

**Acceptance.** Offline tests prove: with `write_back` unset, no write call is attempted for any
source; with it set and a capable adapter, a comment and a transition are issued; with an
incapable adapter, the operation reports `Unsupported` rather than silently no-op'ing; a
description cleared to `""` is distinguishable from one left absent. `pytest tests/unit/test_pm_*`
stays green.

- max attempts: 2

---

## Board shows imported tickets (id: board-shows-imported-tickets)

Added 2026-10-13. The adapters above fill the PM mirror, but no page read it: the Board's Tasks
tab (`d7450c87`) draws only mini-ork's own epics, so an imported Linear or Jira ticket was visible
nowhere in the IDE. This epic puts mirror tickets on the same four columns as the epics, reading
only the mirror (no network at page build), with an explicit empty state that names
`config/pm.yaml` when no source is configured.

Kickoff: `kickoffs/auto/board-shows-imported-tickets.md` (queued with `mini-ork epics add`).
