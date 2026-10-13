# Roadmap — ContextNest in the mini-ork IDE

## About this roadmap (context)

Companion to `docs/epics/EPIC-contextnest-ide-integration.md`, which holds the discovery
evidence, the design rules (§5) and the two tiers (§6). Each `##` section below with an
`(id: …)` is one deliverable, written to be dispatched on its own.

How to use it:

- **Ingest:** `bin/mini-ork epics ingest docs/epics/roadmap-contextnest-ide.md` puts the 16
  epics on the Tasks board; `bin/mini-ork epics split …` writes their kickoffs.
- **Fix the kickoffs' checks before dispatch.** `split` synthesizes `## Verification commands`
  from `py_compile` of the paths it finds. Replace that section in each kickoff with the
  epic's **Verification** list below — those are the real acceptance commands.
- **Dispatch the engine epics only through the scheduler** (`scheduler --epic <id>`). The
  two Rust epics (`cn-ui-editor-file-chip`, `cn-ui-field-map`) are edited in the `mini-ork-ui`
  repo and verified in the `zed-mini-ork` workspace; do them by hand.
- **Order:** `cn-ui-client-tiers` first (every epic reads through it), then
  `cn-ui-session-page` (the drill-down every card opens), then the operator's top jobs.
- **Rules every epic inherits** (overview §5): scope by run, file or project paths, never a
  global feed; slow is not down; fail visibly; never execute ContextNest text; writes are
  operator-initiated and confirm-gated; rewards follow verdicts; show score and source.

## A ContextNest read layer the IDE can trust, on both tiers (id: cn-ui-client-tiers)

- recipe: framework-edit

**Job.** Foundation: every other `cn-ui-*` epic reads ContextNest through this.

**Deliverable.**

1. `cn_client.retrieve` sends `top_k` — ContextNest reads `top_k` (`src/api/tools.rs:79`),
   so today's `limit` key is silently ignored. Keep the Python parameter name.
2. One config file chooses the tier: `config/contextnest.example.yaml` documents
   `tier: simple|secure`, `base_url`, `token_key` (the secret store **key name**, never the
   value), `timeout_ping_s`, `timeout_read_s`, `cache_ttl_s`; a real
   `config/contextnest.yaml` overrides, and `CN_BASE_URL` / `MO_DISABLE_CN` still win.
   Secure tier: the Bearer token is resolved by key name through the same secret store
   provider keys use, sent as a header, never logged, never in a URL or argv. A missing token
   is a hard error naming the key — never a silent fall back to the simple tier.
3. `mini_ork/ide_pages/_cn.py`: a section helper for pages — fetch with the read timeout,
   keep the last good result per (section, scope) under the home's cache directory, label it
   "as of HH:MM" when the live fetch fails, render one line
   "ContextNest is not running — start `contextnest serve`" when it is unreachable with
   nothing cached, and "disabled (MO_DISABLE_CN=1)" when disabled. The availability ping keeps
   its own short timeout; a slow read is not reported as down.
4. Scoping helpers: keep only items whose `files[]` (or text) name a path inside the
   project; extract `run-<digits>-<digits>` ids from text; `sessions_by_file_recent(path)`
   that orders sessions by last activity client-side (ContextNest sorts without recency).
5. A `mini-ork cn` subcommand module, `mini_ork/cli/cn_cmd.py`, registered in
   `mini_ork/cli/main.py`, exposing `build_parser` and one verb, `cn status` (tier, endpoint,
   reachable, latency). Later epics add their write verbs here.

**Scope:** `mini_ork/cn_client.py`, `mini_ork/ide_pages/_cn.py`, `mini_ork/cli/cn_cmd.py`,
`mini_ork/cli/main.py`, `config/contextnest.example.yaml`,
`tests/unit/test_cn_ui_client_tiers.py`.

**Acceptance.** Tests prove: the retrieve body carries `top_k`; the secure tier without its
token raises an error naming the key and the token never appears in a repr, log line or
URL; a slow fetch with a cached result renders the cached section with its "as of" label; an
unreachable service with no cache renders the one-line message; `MO_DISABLE_CN=1` renders
the disabled line; the scope helper drops items with no project path; `cn status` runs
offline.

**Verification.**
- `python3.11 -m pytest -q tests/unit/test_cn_ui_client_tiers.py tests/unit/test_cn_client_py.py tests/unit/test_ide_pages_actions.py`

## One page for any ContextNest session (id: cn-ui-session-page)

- recipe: framework-edit
- depends on: cn-ui-client-tiers

**Job.** Every memory the IDE shows must open its source session (rule 7); this is that page.

**Deliverable.** A page `cn_session` (`mini_ork/ide_pages/cn_session.py`, registered in
`mini_ork/ide_pages/__init__.py`), argument `id`:

1. Header: goal, project, last activity; a button that opens the mini-ork run when the
   session ran inside a run directory.
2. Summary (`/sessions/:id/summary`) as markdown.
3. Trajectory (`/sessions/:id/trajectory`) grouped: decisions, assumptions, failures,
   verifications, open user actions.
4. Delivered features with files and `how_to_test` — shown, never executed (rule 4); a
   "Re-test" button seeds a thread with the recipe so the operator reviews it first.
5. Files touched, each opening the file when it is inside the project.

Never call the raw `/sessions/:id` dump — it takes 12–20 s on large sessions. Links from
other pages use the existing `S.page_link("cn_session", id=…)`; no `spec.py` change.

**Scope:** `mini_ork/ide_pages/cn_session.py`, `mini_ork/ide_pages/__init__.py`,
`tests/unit/test_cn_ui_session_page.py`.

**Acceptance.** Against a stubbed client: each section renders from fixture JSON; a
run-directory session gets the run button; `how_to_test` appears only as text and a thread
action; with ContextNest down the page is still `ok` and shows the one-line state; the page
builds in the IDE actions sweep.

**Verification.**
- `python3.11 -m pytest -q tests/unit/test_cn_ui_session_page.py tests/unit/test_ide_pages_actions.py`

## What needs me: agent sessions' asks on the Inbox (id: cn-ui-needs-you)

- recipe: framework-edit
- depends on: cn-ui-client-tiers
- depends on: cn-ui-session-page

**Job.** Rank 1 — "Which sessions are blocked on me, and on which run?" Today this lives in a
separate web app, and the mini-ork scope reaches ContextNest's 500-item cap in about two days.

**Deliverable.** A section "From agent sessions" on the Inbox page
(`mini_ork/ide_pages/inbox.py`): `/inbox` with `urgency`, plus `/sessions/attention`, scoped
to this project; bucketed now / soon / later; at most 30 rows. Each row: the ask, why, the
session (opens `cn_session`) and, when the text names a run id, a button that opens that run.
A page chip shows the "now" count.

Resolving an item is out of scope until ContextNest adds a resolve route (overview §8); do not
fake it with `tools/discard`, which loses history.

**Scope:** `mini_ork/ide_pages/inbox.py`, `tests/unit/test_cn_ui_needs_you.py`.

**Acceptance.** Fixture items from another project are dropped; buckets and the 30-row cap
hold; a row naming `run-123-456` carries an open-run action; with ContextNest down the
existing inbox renders unchanged plus the one-line state.

**Verification.**
- `python3.11 -m pytest -q tests/unit/test_cn_ui_needs_you.py tests/unit/test_ide_pages_inbox_kanban.py`

## Seen this failure before? (id: cn-ui-seen-before)

- recipe: framework-edit
- depends on: cn-ui-client-tiers
- depends on: cn-ui-session-page

**Job.** Rank 2 — "Have we hit this before, and what fixed it?" Failure memories rarely carry
run ids (3 of 200 sampled), so matching is semantic and must show its evidence.

**Deliverable.**

1. On a failed or escalated run outcome (`mini_ork/ide_pages/outcome.py`) and on a failed
   node (`mini_ork/ide_pages/node.py`): take the failing node's error tail from its run
   artifacts, `retrieve` with `top_k=5` filtered to failure kinds and scoped to the project,
   and render at most three cards — memory, score, source session, age.
2. Fetch once per run and store the result in the run directory, so reopening is instant and
   works with ContextNest down; a failure does not change after the fact.
3. "Helped" / "Wrong" buttons call a new verb `mini-ork cn outcome <atom-id> success|failure
   --run <run-id>` (confirm-gated) that posts to `/agent/outcome`; on the secure tier it needs
   a write-scoped token.

**Scope:** `mini_ork/ide_pages/outcome.py`, `mini_ork/ide_pages/node.py`,
`mini_ork/cli/cn_cmd.py`, `tests/unit/test_cn_ui_seen_before.py`.

**Acceptance.** A fixture failure yields three scored cards each opening its session; a second
open reads the stored result without a fetch; the outcome verb posts the documented body and
reports ContextNest's `updated` field; the buttons resolve in the actions sweep; ContextNest
down with nothing stored renders the one-line state.

**Verification.**
- `python3.11 -m pytest -q tests/unit/test_cn_ui_seen_before.py tests/unit/test_ide_pages_actions.py`

## Reward memories by verdict, not by completion (id: cn-ui-verdict-outcome)

- recipe: framework-edit
- depends on: cn-ui-client-tiers

**Job.** Make ContextNest's ranking honest. `hooks/subagent-stop.sh` marks every prefetched
atom `success` when a subagent finishes — including runs that are later rejected — so it
reinforces the memories that led to bad work.

**Deliverable.**

1. `hooks/subagent-stop.sh` stops posting outcomes; its other work is unchanged.
2. Where the run-level verdict is finalised (the code that writes `run-verdict.json`), post
   an outcome for each ContextNest atom the run was given (the `learned/<node>.json` sources
   and the prefetch record): `success` when the run publishes, `failure` on reject or
   rollback, nothing when it abstains or is interrupted.
3. Record what was posted in the run directory so a second settle posts nothing; when
   ContextNest is down, record the outcomes as pending instead of failing the run.

**Scope:** `hooks/subagent-stop.sh`, `mini_ork/cn_client.py`,
`tests/unit/test_cn_ui_verdict_outcome.py`.

**Acceptance.** Tests prove: subagent completion posts nothing; a passing published run posts
one `success` per atom; a rejected run posts `failure`; abstain and interrupted post nothing;
settling twice does not post twice; ContextNest down leaves a pending record and the run's
verdict untouched.

**Verification.**
- `python3.11 -m pytest -q tests/unit/test_cn_ui_verdict_outcome.py tests/unit/test_cn_client_py.py`

## What the run's agents decided, and the history of the files it changed (id: cn-ui-run-memory)

- recipe: framework-edit
- depends on: cn-ui-client-tiers
- depends on: cn-ui-session-page

**Job.** Rank 3 — reviewing a verdict or diff before merge. Run nodes are ingested
(435 sessions live under `.mini-ork/runs`), and `mini_ork/ide_pages/node.py` already maps a
node to its session.

**Deliverable.**

1. On the run page (`mini_ork/ide_pages/run.py`), a "Decisions & assumptions" section: for
   each node session, the `decision_made`, `assumption` and `risk_flag` atoms from its
   trajectory, labelled with the node, each opening `cn_session`.
2. On the run's changed-files view, a history line per file — "N sessions · last <age>" —
   from the recency-ordered helper, expandable to the list.
3. Once a run is finished, store both in the run directory; they no longer change.

**Scope:** `mini_ork/ide_pages/run.py`, `mini_ork/ide_pages/node.py`,
`tests/unit/test_cn_ui_run_memory.py`.

**Acceptance.** A fixture run with two node sessions lists their decisions with node labels;
file history is ordered by recency, not by ContextNest's order; a finished run reads its
stored data without fetching; the existing run-page tests still pass.

**Verification.**
- `python3.11 -m pytest -q tests/unit/test_cn_ui_run_memory.py tests/unit/test_ide_pages_run_story.py`

## See and settle overlaps between concurrent sessions (id: cn-ui-coordination)

- recipe: framework-edit
- depends on: cn-ui-client-tiers

**Job.** Rank 4 — "Is another run editing my files, and who yields?" ContextNest holds 140
overlaps, 134 open and 1 acknowledged: the ack loop is unused because no UI offers it.

**Deliverable.** On the Coordination tab (`_coord` in `mini_ork/ide_pages/orch.py`), next to
the existing principals, hot claims and owns violations:

1. An "Overlaps" table from `/coord/overlaps`, open first — kind, paths, principals, age —
   with an "Ack" button (confirm-gated) calling a new verb `mini-ork cn ack-overlap <id>`
   that posts to `/coord/overlaps/:id/ack`.
2. Active path freezes from `/coord/freezes`.
3. "Duplicate work" from `/coord/topic-pairs`: pairs of live sessions working on the same
   thing, each side opening its session.

**Scope:** `mini_ork/ide_pages/orch.py`, `mini_ork/cli/cn_cmd.py`,
`tests/unit/test_cn_ui_coordination.py`.

**Acceptance.** Open overlaps sort first and cap at 30; the Ack button carries `confirm` and
resolves in the actions sweep; the ack verb posts to the overlap's path; topic pairs render
both sides; with ContextNest down the tab keeps its existing one-line fallback.

**Verification.**
- `python3.11 -m pytest -q tests/unit/test_cn_ui_coordination.py tests/unit/test_ide_pages_actions.py`

## Shipped since yesterday, with how to re-test it (id: cn-ui-shipped-today)

- recipe: framework-edit
- depends on: cn-ui-client-tiers
- should follow: cn-ui-needs-you

**Job.** Rank 7 — start of day. `/features?since=24h` held 91 entries on 2026-10-13, each
with a replay recipe; its `project` filter matches session cwd, so filter by `files[]`.

**Deliverable.** A "Shipped since yesterday" section on the Inbox page
(`mini_ork/ide_pages/inbox.py`): features whose files lie inside this project — feature,
files, session (opens `cn_session`), and the `how_to_test` text. A "Re-test" button seeds a
thread with the recipe; nothing runs without the operator sending it (rule 4).

**Scope:** `mini_ork/ide_pages/inbox.py`, `tests/unit/test_cn_ui_shipped_today.py`.

**Acceptance.** Out-of-project features are dropped even when their session cwd matches; the
recipe appears only as text and as a thread action; ContextNest down renders the one-line
state.

**Verification.**
- `python3.11 -m pytest -q tests/unit/test_cn_ui_shipped_today.py tests/unit/test_ide_pages_inbox_kanban.py`

## An honest Context page (id: cn-ui-context-page)

- recipe: framework-edit
- depends on: cn-ui-client-tiers

**Job.** Today the page can say "connected" while the planner receives nothing, and its
"Cache" panel is mini-ork's local counters, not ContextNest's cache.

**Deliverable.** In `mini_ork/ide_pages/context.py`:

1. Status from `/substrate/health`, `/substrate/config` and `/substrate/replay`, with
   reachable and slow shown separately. Do not call `/substrate/consolidation`,
   `/field/basins` or `/fragments`: they hang at current scale.
2. "What the planner receives": whether `MO_PLANNER_SHARED_CONTEXT` is on, and which role
   packs are implemented (planner only today) against what is documented.
3. A collapsed preview of this project's prompt capsule (`/prompt-context/capsule`, last
   14 days) with a size estimate — the capsule has no UI anywhere yet.
4. Rename the local panel to "Context reuse (local)" and add "ContextNest LLM cache" from
   `/llm/v1/cache/stats`, zeros shown as zeros.

Bring `docs/CONTEXTNEST-INTEGRATION.md` in line with the code: the undocumented `coord_*`
functions, the graph endpoints, and the planner-only role-pack status.

**Scope:** `mini_ork/ide_pages/context.py`, `docs/CONTEXTNEST-INTEGRATION.md`,
`tests/unit/test_cn_ui_context_page.py`.

**Acceptance.** With the shared-context flag off, the page says the planner receives no
ContextNest context even when connected; the capsule section renders fixture markdown
collapsed; the two cache panels are labelled distinctly; no request goes to the three hanging
routes.

**Verification.**
- `python3.11 -m pytest -q tests/unit/test_cn_ui_context_page.py tests/unit/test_ide_pages_actions.py`

## Prior art on a task before it is queued (id: cn-ui-task-prior-art)

- recipe: framework-edit
- depends on: cn-ui-client-tiers
- depends on: cn-ui-session-page

**Job.** Rank 6 — "Was this done or tried before?" Prevents duplicate and hollow tasks.

**Deliverable.** When `/task` or `/kickoff` shows its draft card
(`_offer_kickoff_draft_approval` in `mini_ork/acp/agent.py`), compute prior art once for the
draft title — `sessions_by_feature` (unused until now) plus failure atoms from the capsule —
and add at most three lines to the draft preview, each naming its session. Never on
keystrokes. With ContextNest down, add one line saying prior art was skipped and why.

**Scope:** `mini_ork/acp/agent.py`, `tests/unit/test_cn_ui_task_prior_art.py`.

**Acceptance.** A fixture match adds three or fewer lines with session ids; the lookup runs
once per draft; ContextNest down adds the skipped line and the draft card still works; the
`/task` tests still pass.

**Verification.**
- `python3.11 -m pytest -q tests/unit/test_cn_ui_task_prior_art.py tests/unit/test_acp_task_command.py`

## File history page (id: cn-ui-file-history)

- recipe: framework-edit
- depends on: cn-ui-client-tiers
- depends on: cn-ui-session-page

**Job.** Rank 5, engine half — "Who touched this file, why, and is anyone in it now?" The
editor chip (`cn-ui-editor-file-chip`) opens this page.

**Deliverable.** A page `cn_file` (`mini_ork/ide_pages/cn_file.py`, registered in
`mini_ork/ide_pages/__init__.py`), argument `path` (project-relative):

1. Sessions that touched the file, newest first, each with goal, feature and last activity,
   opening `cn_session`.
2. Who holds it now, from `/coord/hot-claims`.
3. Recent failures that name the file.

Paths outside the project render an explanation, not data.

**Scope:** `mini_ork/ide_pages/cn_file.py`, `mini_ork/ide_pages/__init__.py`,
`tests/unit/test_cn_ui_file_history.py`.

**Acceptance.** Sessions are ordered by recency regardless of ContextNest's order; a live
claim shows its principal; an outside path shows no data; the page builds in the actions
sweep.

**Verification.**
- `python3.11 -m pytest -q tests/unit/test_cn_ui_file_history.py tests/unit/test_ide_pages_actions.py`

## Editor chip: this file's history (id: cn-ui-editor-file-chip)

- depends on: cn-ui-file-history

**Job.** Rank 5, editor half. Rust in the `mini-ork-ui` repo — dispatch by hand.

**Deliverable.** The crate has no hook on the open editor buffer today: `ActiveItemChanged` is
used only to follow run tabs. Add one that follows the active editor's project-relative path,
and a status-bar item (beside the existing ones in `status.rs`) reading "N sessions" for it.
Debounce on tab switches and cache per path; hide it for paths outside the project. Clicking
opens the `cn_file` page for that path. The count comes from the engine, through the same
`mini-ork` call the board already uses — no direct HTTP from Rust to ContextNest.

**Acceptance.** Switching between two files updates the count once each; a path outside the
project hides the item; clicking opens `cn_file` with the right argument; ContextNest down
hides the count instead of showing zero.

**Verification.** In `/Volumes/docker-ssd/ps/zed-mini-ork`:
- `cargo test -p mini_ork_ui`
- `./script/clippy` (new code adds no warnings)

## Search what the substrate knows (id: cn-ui-memory-search)

- recipe: framework-edit
- depends on: cn-ui-client-tiers
- depends on: cn-ui-session-page
- should follow: cn-ui-context-page

**Job.** Rank 9 — exploring memory without leaving the IDE.

**Deliverable.** A "Search" tab on the Context page (`mini_ork/ide_pages/context.py`) with
four modes — memories (`retrieve`), sessions (`sessions/by-feature`), features, files
(`sessions/by-file`). The query comes from the existing composer section, which runs a new
verb `mini-ork cn search <mode> <query>`; it stores the result in the home cache and the tab
renders the latest search. Results are scoped to this project; searching all projects takes
an explicit, labelled chip (privacy, overview §5.1).

**Scope:** `mini_ork/ide_pages/context.py`, `mini_ork/cli/cn_cmd.py`,
`tests/unit/test_cn_ui_memory_search.py`.

**Acceptance.** Each mode renders fixture results that open `cn_session`; default results
exclude other projects; the all-projects chip is off by default and labelled; the composer's
action resolves in the actions sweep.

**Verification.**
- `python3.11 -m pytest -q tests/unit/test_cn_ui_memory_search.py tests/unit/test_ide_pages_actions.py`

## Past decisions on lanes and recipes (id: cn-ui-lane-decisions)

- recipe: framework-edit
- depends on: cn-ui-client-tiers

**Job.** Rank 8 — "What did I already decide for this kind of work?" Learned routing stays
the source of truth; ContextNest adds the human decisions and caveats behind it.

**Deliverable.** A "Past decisions" section on the Lanes page (`mini_ork/ide_pages/lanes.py`):
decision and failure atoms from the capsule for lane and budget queries over the last
14 days, scoped to the project, each opening its session.

**Scope:** `mini_ork/ide_pages/lanes.py`, `tests/unit/test_cn_ui_lane_decisions.py`.

**Acceptance.** Fixture decisions render with their session; out-of-project items are dropped;
ContextNest down renders the one-line state.

**Verification.**
- `python3.11 -m pytest -q tests/unit/test_cn_ui_lane_decisions.py tests/unit/test_ide_pages_actions.py`

## Board badges for asks and overlaps (id: cn-ui-board-badges)

- recipe: framework-edit
- depends on: cn-ui-needs-you
- depends on: cn-ui-coordination
- should follow: board-shows-imported-tickets

**Job.** Put the two attention signals on the board where the operator already looks.

**Deliverable.** On the Board's Tasks and Runs cards (`mini_ork/ide_pages/kanban.py`), a meta
item for open ContextNest asks that name the card's run, and one for open overlaps on its
paths, using only the card fields the IDE already renders. Values come from the cached
sections of the two earlier epics; the board never waits on ContextNest.

**Scope:** `mini_ork/ide_pages/kanban.py`, `tests/unit/test_cn_ui_board_badges.py`.

**Acceptance.** A run named by a fixture ask gets the badge; with ContextNest down the board
renders exactly as before; the board tests still pass.

**Verification.**
- `python3.11 -m pytest -q tests/unit/test_cn_ui_board_badges.py tests/unit/test_ide_pages_kanban_epics.py tests/unit/test_ide_pages_inbox_kanban.py`

## Semantic field map with a query overlay (id: cn-ui-field-map)

- depends on: cn-ui-client-tiers
- related to: cn-ui-memory-search

**Job.** ContextNest's most IDE-relevant view: type a draft prompt and the memories it would
pull light up. Rust in the `mini-ork-ui` repo — dispatch by hand.

**Blocked** until ContextNest serves `/field/basins` at current scale (it hangs today) and
exposes projected coordinates; file both in the ContextNest repo (overview §8).

**Deliverable.** A canvas section in the crate for points, basin halos and co-retrieval
edges, with a query overlay that dims everything but the matches; the engine page supplies
the projected points. Until the blocker clears, no work starts.

**Verification.** In `/Volumes/docker-ssd/ps/zed-mini-ork`:
- `cargo test -p mini_ork_ui`
