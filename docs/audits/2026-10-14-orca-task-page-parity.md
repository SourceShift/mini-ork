# From board card to task page — what Orca has, what mini-ork has, and the gap

**Date:** 2026-10-14
**Scope:** the mini-ork IDE Board (`ide_pages/kanban.py`) versus Orca's task surface
(`sources/orca`, its Linear / Jira / GitHub / GitLab task pages).
**Purpose:** a discovery + decision list, not a plan. Nothing here is scheduled.

---

## 1. Why this exists

Clicking a card in the Board's Tasks columns does not open a task. Today a card's
`do` action is one of exactly three things (`kanban.py:250-255`, `:368-372`):

| card kind | `do` when… |
|---|---|
| epic | the epic has an `epic_attempts` run → `open_run`; else its kickoff file is on disk → `open_path`; else **no `do` key, no click handler** |
| mirror ticket | a `run:` link exists → `open_run`; else the ticket has a URL → `url` (leaves the IDE); else **nothing** |

So the click either jumps sideways to a run, or leaves the app for the source's
own web UI. There is no place in mini-ork that shows *the task itself*: its
description, its timestamps, its comments, its links to runs.

Orca has exactly that surface. This document inventories it, then lists what
mini-ork would need.

---

## 2. The Board as it is today

`mini_ork/ide_pages/kanban.py` — page key `kanban`, label **Board**, two tabs
(`kanban.py:41`).

**Tasks tab** (`_tasks_board`, `kanban.py:528`) — a `S.columns` section of four
columns, `Backlog / Working / Needs you / Done` (`_TASK_COLUMNS`, `:109`), filled from two sources:

- **Epics** from the local `epics` table (`_epic_buckets`, `:442`) — status →
  column via `_task_column_for` (`:168`); a `blocked` epic with a `held_reason`
  is a scheduler hold and stays out of Backlog; `in review` and any unknown
  status land in Needs you under a *never silently dropped* rule.
- **Tickets** from the PM mirror (`_tickets_into`, `:494`) — `pm_issues` rows via
  `mini_ork.pm.store`, `local` rows skipped (already drawn as epics), one
  `provider` chip and an `agent` chip when a `run:` link exists.

Header actions (`kanban.py:591-599`): **New task** (`thread("/task ")`),
**Sync tickets** (`cli intake once --dry-run`), and All / Epics / one button per
provider (`_source_actions`, `:418`). A freshness line above the columns reports
each source's last sync or its recorded error (`_freshness_items`, `:381`).

**Runs tab** (`_board`, `:90`) — fleet cards in `Working / Needs you / Failed / Done`
via `mini_ork.acp.fleet.fleet_rows`, Done over a rolling 24 h window.

**The card** is `{id, title, sub, state, mark, meta, do}` (`_task_card` `:221`,
`_ticket_card` `:349`). Sort is priority DESC then recency (`_sort_task_cards`
`:264`), capped at 30 per column (`_TASK_CAP`).

**Load-bearing design rule** (`kanban.py:11-14`): *"Mirror tickets are read
through `mini_ork.pm.store` only — the page makes no network call and imports no
adapter."* A slow or broken source can neither slow nor blank the board. The
`server` tests pin this. **Any detail page that shows a description or comments
breaks this rule**, because the mirror stores no body (see §5).

---

## 3. Orca's task surface — the reference

Repo: `/Volumes/docker-ssd/ps/zed-mini-ork/sources/orca`. TypeScript/Electron.
Providers: **github · gitlab · linear · jira** (`src/shared/task-providers.ts:1`).
There is **no internal "Orca issue" entity** — issues are always a live
projection of a provider, held in renderer memory.

### 3.1 Views and layout

| capability | anchor |
|---|---|
| Provider dispatch (which list you get) | `task-page/Content.tsx:28-74` |
| Linear modes: Issues / Projects / Views / **Has Workspace** | `task-page-localized-options.tsx:150-160` |
| List ⇄ **Board (kanban)** toggle | `task-page/linear/IssueToolbar.tsx:84-116` |
| Board columns grouped by status, drag-to-move | `use-task-page-linear-board.ts:49,75-224` |
| Table view with sticky columns | `task-page/linear/IssueTable.tsx:28-279` |
| Group by: none / status / assignee / priority / team | `shared/linear/issue-view-resume-state.ts:15` |
| Order by: priority / updated / identifier | `issue-view-resume-state.ts:16` |
| Display toggles: state, priority, assignee, team, labels, updated | `issue-view-resume-state.ts:17-24` |
| Attribute filters (status/priority/assignee/labels) as removable pills | `linear-issue-attribute-filter-dropdowns.tsx:307-422` |
| Per-provider search (Linear text, GitHub qualifier DSL, Jira **JQL**) | `issue-view-resume-state.ts`, `shared/task-query.ts:1`, `shared/jira-search-input-jql.ts` |
| Pagination "Load more" | `task-page/PaginationBar.tsx:29-84` |
| Layout survives restarts (localStorage / resume state) | `linear-issue-view-storage.ts:10-34`, `shared/ui-chrome-types.ts:74` |
| Empty / loading-skeleton / offline banner per provider | `task-page/linear/IssueBoard.tsx:47-176`, `github/List.tsx:125-271` |
| Keyboard: Esc closes, ⌘F focuses search, ⌘↵ submits, rows Enter/Space | `use-task-page-global-effects.ts:30-91`, `use-task-page-search-actions.ts:183-238` |

### 3.2 The detail view

Two variants of one component — full page and right-side sheet
(`LinearIssueWorkspace.tsx:76-159`; drawer at `linear-item-drawer-sheet.tsx:67-325`).

Shown: breadcrumb + **Copy URL / Open on Linear / Start-or-Open workspace**
(`linear-issue-workspace-header.tsx:48-194`); inline **title** editor;
**markdown description** editor (Tiptap with a formatting toolbar and table
controls, `LinearIssueMarkdownDescriptionEditor.tsx:79-118`); **comments thread**
(render + composer, optimistic add — `linear-issue-activity.tsx:105-139`,
`linear-item-drawer-comment-footer.tsx:55-108`); **activity feed** showing
"updated the issue · 3 h ago" (`linear-issue-activity.tsx:53-79`); **sub-issues**
with a create popover (`linear-issue-sub-issues.tsx:153-219`); a **properties
sidebar** for state / priority / assignee / estimate / labels / project
(`linear-issue-workspace-sidebar.tsx:148-166`); an **Actions card** — copy URL,
copy identifier, copy suggested branch name, **copy prompt-as-context**
(`linear-issue-workspace-sidebar.tsx:54-126`).

### 3.3 Write actions

Create dialogs with distinct fields per provider — Linear (team, title,
description, labels/assignee; `IssueDialog.tsx:49-136`), GitHub (project, title,
markdown body, labels, assignees), Jira (**dynamic required custom fields**,
`task-page/jira/IssueDialog.tsx:96-298`). Drafts survive an accidental dismiss
(`task-page-draft-storage.tsx:33-56`).

Inline edits are optimistic with revert-on-error (`linear-item-drawer-edit-controller.tsx:55-277`).
Comments can be **added** on all four providers. Types are
`LinearIssueUpdate` (`shared/issue-mutation-types.ts:31-42`), `GitHubIssueUpdate`
(`:12-25`), `GitLabIssueUpdate`, `JiraIssueUpdate` (`shared/jira-types.ts:131`).

### 3.4 Backend capability per provider (the honest asymmetry)

| | Linear | Jira | GitHub | GitLab |
|---|---|---|---|---|
| list / get / search | ✅ / ✅ / ✅ | ✅ / ✅ / JQL | ✅ / ✅ / DSL | ✅ / ✅ / — |
| create | ✅ incl. sub-issue | ✅ + custom fields | ✅ | ✅ |
| update fields | ✅ 10 fields | title, labels, priority, assignee, transition | state, reason, dup, title, body, labels, assignees | state, body, title, labels, assignees |
| transition | ✅ stateId | ✅ **transitionId** | open/close | close/reopen |
| comment | ✅ (no edit/delete) | ✅ (no edit/delete) | ✅ | ✅ (note) |
| edit/delete comment | ❌ | ❌ | ✅ project path only | ❌ |
| relations | ✅ blocks/blocked-by/related/duplicate | ❌ | ❌ | ❌ |
| attachments | link-only | images read-only | — | ❌ |
| activity history | ✅ | — | ✅ timeline | — |
| estimate / cycle / milestone | ✅ | ❌ | ❌ | ❌ |
| reactions | ❌ | ❌ | ✅ comment reactions | ❌ |
| auth | personal API key | site + email + token (cloud) / PAT-or-basic (server) | reuses `gh` login | reuses `glab` login |
| secret at rest | `safeStorage` ciphertext, 0600 fallback (`main/linear/linear-token-store.ts:37`) | same (`main/jira/site-credential-store.ts:201`) | via `gh` | via `glab` |

Anchors: `main/linear/linear-issue-mutations.ts:19,155`, `main/linear/issue-relation-write.ts:25`,
`main/jira/jira-issue-mutations.ts:38,86,121,139`, `main/jira/jira-transition-queries.ts:15`,
`main/github/issue-create.ts:23`, `main/github/issue-update.ts:12`,
`main/gitlab/issue-update.ts:25`.

### 3.5 Connecting a source from the UI (what the user asked to see)

Three entry points, one dialog each, no config file ever touched by hand.

**Linear** — `LinearApiKeyDialog` (`components/linear-api-key-dialog.tsx`):
title **"Add Linear access"** (`:117`); one `type="password"` field
(placeholder `lin_api_…`); inline help that deep-links to *Account → Security &
Access* and to *Workspace API settings* (`:204-224`); a lock line stating where
the key is stored — *"stored on this device using Electron encrypted storage"*
locally, *"stored by the active remote runtime"* remotely (`:123-126`); the
parent is blocked from dismissing the dialog once a key is typed
(`preventOutsideDismissWhenDirty`); Enter submits; button label **"Verifying…"**
while `connectLinear` runs (`store/slices/linear/linear-slice-connection-actions.ts:82`).
Multi-workspace: connect several, `selectWorkspace('all')`, per-workspace
disconnect (`main/linear/linear-workspace-registry.ts:223`).

**Jira** — `JiraConnectDialog` (`components/jira-connect-dialog.tsx`): a
Cloud/Server toggle, then either *site URL + email + API token* or, for
self-hosted, **PAT or username+password** (`ServerAuthMethod`, `:34-50`); the
form resets on every open so a stale secret never renders (`:63-72`); stored the
same way, site metadata in `~/.orca/jira-sites.json`, token in
`~/.orca/jira-tokens/<id>.enc`.

**Where they live in the UI** — Settings → Integrations
(`settings/IntegrationsPane.tsx:31-53`: GitHub, GitLab, Bitbucket, Azure DevOps,
Gitea, then **Linear**, **Jira**), each card offering Connect / **Test** /
Disconnect / per-workspace rows (`task-tracker-integration-cards.tsx:20-120`);
plus a **guided 3-step setup** (Connect → install the agent skill → show in
Tasks) in `settings/TaskSourceLinearSetup.tsx:37-100`; plus an onboarding step
(`onboarding/IntegrationsStep.tsx:179-216`).

Two details worth copying: **Test is an explicit, user-triggered action** —
*"the only settings path that decrypts a stored Linear key, avoiding surprise
keychain prompts"* (`task-tracker-integration-cards.tsx:50-52`); and an
`UnsealedCredentialNotice` when a token is stored in the 0600 plaintext
fallback rather than under the OS keyring.

---

## 4. What mini-ork already has (more than Orca in places)

The engine's PM spine is already built and is *provider-neutral*:

- **`mini_ork/pm/model.py`** — `Issue` (identity, title, `state` + `native_state`,
  `description`, `priority` + `native_priority`, labels, assignee, `parent_uid`,
  **`created_at`**, **`updated_at`**, `blocked_reason`, `native` payload),
  `Comment`, `Link` / `LinkKind` (**blocks, relates, duplicates, parent, run**),
  and a closed 7-value `IssueState`. Priority counts *up*.
- **`mini_ork/pm/provider.py`** — `Capabilities` (create, update, transition,
  comment, link, search, states, priorities, labels, assignees, pagination) and
  the `Provider` protocol: `list_issues`, `get`, `comments`, `create`, `update`,
  `transition`, `comment`, `link`.
- **Four adapters** — `local` (epics), `github`, `jira`, `linear`
  (`pm/providers/__init__.py:33-37`). Honest capabilities: Linear/Jira
  `transition`+`comment` true, create/update/link false; GitHub read-only; local
  create/update/link/search.
- **`mini_ork/pm/store.py`** — the mirror: `pm_issues`, `pm_links`, `pm_sync`
  (migration `0069_pm_mirror.sql`); `sync`, `query_rows`, `row(uid)`,
  `mirror_state`, `link`, `links_for`.
- **`mini_ork/pm/writeback.py`** — *the one gate every outbound mutation passes
  through*: `write_back: true` in the source's `config/pm.yaml` stanza **and** the
  adapter's `Capabilities` flag, else `Unsupported` and **no adapter call**.
  Entry points `comment` / `transition` / `update` exist and have **no callers yet**.
- **`mini_ork/pm/config.py`** — `config/pm.yaml`: `id`, `enabled`, `token_env`
  (a **name**, never a value), `settings`; fails closed on a blank key name.
- **`mini_ork/dispatch/vault.py`** — one name → one value, backends `env` → `file`
  (`secrets.local.sh`, 0600) → `sops` → `command` (pass/op/bws/infisical) →
  `openbao`. The two delivery tiers of `CLAUDE.md`, already implemented. **Read-only.**
- **`mini_ork/intake/`** — `poll_once` / `poll_loop` / `source_statuses`, `trigger.py`
  (opt-in rules: `trigger_label`, `trigger_assignee`, `trigger_states`,
  `trigger_recipe` — *a source with no rules starts nothing*), signed intake
  webhooks, and a CLI: `mini-ork intake once|poll|status`.

**Gaps inside that spine:** `mini-ork pm sources` is promised by
`pm/providers/__init__.py:55` but **no `pm` subcommand is registered** in
`cli/main.py`; `writeback` has no caller; and the mirror deliberately stores
**no `description`, no `created_at`, no comments** (`store.py:102-105`: *"A view
that needs them re-reads the issue by uid"*).

---

## 5. The gap list, grouped for discussion

### A. Make a card clickable into a task

| # | item | where |
|---|---|---|
| A1 | A new `task` IDE page: `mini_ork/ide_pages/task.py` + one line in `PAGES` (`ide_pages/__init__.py:16`), `build(home, tab, args)` keyed on `args["uid"]` | engine |
| A2 | A new spec action `S.open_task(uid, title)` → `{"page": "task", "args": {"uid": …}}`. Needs **no** Rust change: `store.rs:472` already routes a generic `page` action with args | engine |
| A3 | Point ticket cards' `do` at the page, keeping run / URL as secondary actions on the card and in the page | `kanban.py:368-372` |
| A4 | Same for epic cards (`uid` = `local:<epic id>`) so epics get a detail page too | `kanban.py:250-255` |
| A5 | IDE: tab title for a parameterised page. `page.rs:2042` special-cases `key == "run"`; a `task` page needs the same so the tab reads `ENG-123 · title`, not "Task" | zed-mini-ork |
| A6 | Open-in-new-tab vs replace: `page.rs:2084` finds an existing tab by `args["run"]`; the task page should do the same on `uid` | zed-mini-ork |

### B. What the page shows

| # | item | source |
|---|---|---|
| B1 | Markdown body. `S.markdown` accepts raw `text` (no file needed) — but the mirror has no body, so this needs a **live adapter read** (`Provider.get(uid)`) | open question §6.1 |
| B2 | Created + updated time. `Issue.created_at` / `updated_at` exist; the **mirror only stores `updated_at`** | add a column, or read live |
| B3 | Identity block: native key, provider chip, `native_state`, priority (with `Priority` upward semantics), labels, assignee, project, parent | mirror, already present |
| B4 | Blocked/held reason, dependency-blocked hint (epics) | `epics.held_reason`, `epic_dependencies` |
| B5 | Comments thread (read; write is §C) | `Provider.comments` — live, not mirrored |
| B6 | Links: `run:` links → open the run (today's behaviour, kept as a section), `blocks` / `relates` / `parent` | `pm_links` via `links_for` |
| B7 | Runs that worked this task: `epic_attempts` for epics, `run:` links for tickets, with cost/state | `epic_attempts`, `fleet_rows` |
| B8 | Source health: last sync, or the recorded error verbatim | `mirror_state` (already credential-scrubbed at write time) |
| B9 | Actions card, mirroring Orca's: copy URL / copy key / **copy prompt-as-context** / open the linked run / open the kickoff file / open on the source | mix |

### C. Writing back

| # | item | note |
|---|---|---|
| C1 | **Comment** from the page | `writeback.comment` exists; first caller |
| C2 | **Drag a card between columns** (or a status control) | `writeback.transition`; only possible for providers whose `Capabilities.transition` is true |
| C3 | Edit title / description / labels / assignee | `writeback.update` + `Patch`; needs `update=True` in the adapters, which is **false today for Linear/Jira/GitHub** |
| C4 | Create a task from the Board (today: the `/task` thread) | `writeback` has no create gate; `Provider.create(draft)` exists for Linear/Jira/GitHub/GitLab on the Orca side but `create=False` in mini-ork's adapters |
| C5 | Surface *why* an action is unavailable — capability + `write_back` flag, not a hidden button | the `Capabilities` design already intends this (`provider.py:59`) |

### D. Integrate Linear / Jira **from the UI** (the explicit ask)

| # | item | note |
|---|---|---|
| D1 | A **Sources** section (Board header, or Setup) listing every `config/pm.yaml` source: enabled, last sync, error, capability badges | reads `config.py` + `mirror_state` |
| D2 | **Add Linear**: pick provider → one secret field → validate → write | Orca's dialog is the model |
| D3 | **Add Jira**: site URL + email + token, Cloud/Server switch, PAT-vs-basic for Server | per `jira.py`'s actual expectations |
| D4 | **Test connection** as an explicit button, not a side effect of opening the pane | Orca's deliberate choice; matters because a test may decrypt/unseal |
| D5 | **Write the secret**: today `vault.py` is read-only *by design* ("nothing here ever writes a value"). Needs `mini-ork vault set` (or `pm add-source`) writing a 0600 `secrets.local.sh` for the simple tier, and pointing at `sops` / `openbao` for the secure tier — fail-closed both ways, per `CLAUDE.md` | **the real design decision** |
| D6 | **Write `config/pm.yaml`** (or a project-local override) from the page: `id`, `enabled`, `token_env`, `settings` (team key / site / project) | config.py already defines the shape |
| D7 | A real **`mini-ork pm` subcommand** (`sources` / `add` / `test` / `remove`) — already promised in a docstring | `registry.py` has `providers()`, `capabilities()`, `skipped_sources()` |
| D8 | Disconnect / rotate a key (delete the vault entry, mark the source disabled) | — |
| D9 | `intake status` and the trigger rules surfaced where the source is configured, so "why did nothing start" is answerable | `intake/trigger.py` |
| D10 | Setup page currently says *"a credential is never entered through this page"* (`setup.py:526-531`) — this ask changes that stated invariant, deliberately | needs a decision |

### E. Board parity, independent of any detail page

| # | item |
|---|---|
| E1 | Filters by provider / state / assignee / label / priority, as chips (the `source` filter already exists; `S.chips` supports it) |
| E2 | Group-by and order-by controls (today: fixed status columns, fixed priority-then-recency) |
| E3 | List ⇄ Board view modes (Today: Board only) |
| E4 | Pagination — `_TASK_CAP = 30` with no "Load more" |
| E5 | A refresh control and a visible last-synced stamp (the freshness line exists; no button) |
| E6 | Keyboard navigation across cards |

### F. Deliberately **absent** in Orca — decide whether we want them

No issue/task **delete**; no **bulk / multi-select**; no **comment edit or
delete**; no **attachments surface**; **no created-at display** (only
updated-at); no timeline/gantt view; no reactions (except GitHub comment
reactions); no watchers/subscriptions; **no offline cache** — issue data is
in-memory only and dies with the renderer session.

---

## 6. Four decisions I need from you

1. **Does the detail page read live or stay mirror-only?** `kanban.py:11-14`
   states the board makes *no network call* so a broken source cannot blank it.
   A description and a comments thread cannot come from the mirror as it stands.
   Options: (a) a `task` page that reads live but degrades to the mirror row with
   a "source unreachable" notice; (b) mirror-only detail (add `description` and
   `created_at` columns, no comments) — cheaper, no new failure mode, but a
   stale body; (c) keep the board offline and let the *page* be the only online
   surface.

2. **How far into writes do we go?** Read-only detail is a small change.
   Comment + transition are gated and ready (`writeback.py`); `update`/`create`
   additionally need `Capabilities` flipped **and implemented** in the Linear and
   Jira adapters.

3. **Where does the source connect flow live and how does it store the secret?**
   Writing a 0600 `secrets.local.sh` is the simple tier and trivially doable;
   the secure tier must point at `sops`/`openbao` rather than grow a second
   secret path. Do we also want it to write `config/pm.yaml` (convenience) or
   only the secret, leaving the file to a person?

4. **Is the epic card's detail page in scope too?** Epics are the one entity
   mini-ork owns outright — the page would be offline, always available, and
   could carry the scheduler's view (attempts, deps, inbox items) that Orca has
   no equivalent of. It may be the more valuable half of this work.

---

## 7. Anchors

- Board: `mini_ork/ide_pages/kanban.py` (whole file), `mini_ork/ide_pages/spec.py:1-65`
  (page + section + action vocabulary), `mini_ork/ide_pages/__init__.py:16`.
- IDE renderer: `crates/mini_ork_ui/src/page.rs:497-540` (action handler),
  `crates/mini_ork_ui/src/store.rs:461-545` (`perform`), `page.rs:2042-2099`
  (tab identity by arg).
- PM spine: `mini_ork/pm/{model,provider,store,config,writeback}.py`,
  `mini_ork/pm/providers/{local,linear,jira,github}.py`, `mini_ork/dispatch/vault.py`,
  `mini_ork/intake/{poll,trigger,dispatch,webhooks}.py`.
- Orca: `src/renderer/src/components/task-page/**`, `src/shared/task-providers.ts`,
  `src/main/{linear,jira,github,gitlab}/**`, `docs/site/content/docs/review/{linear,jira,github}.mdx`.
