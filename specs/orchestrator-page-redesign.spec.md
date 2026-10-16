# Feature: Orchestrator page — dispatch, intake, decisions and fleet

> **Companion briefs (zed-mini-ork repo, `docs/ux-briefs/`):**
> `2026-10-15-orchestrator-intake.md` (the audit — what is wrong),
> `2026-10-15-orchestrator-intake-redesign.md` (the journey + FE/BE design).
>
> ⚠️ **The line anchors in both companions are stale.** They were taken against
> `mini_ork/ide_pages/orch.py` at 604 lines; it is now **791** and the functions
> have moved (`_arrivals` 485→723, `_planner` 312→312, `build` 565→753). Cite the
> **function name**, never the line, when implementing from them.

## Overview

The `Orchestrator & intake` IDE page (`mini-ork board page orch`) is the project's
control surface for getting work *in* and *unblocked*, but it is **read-only over
things that are writable**. Roughly half the backend it needs already exists and is
simply unreferenced: `orchestrator_lanes()`, `recipes_catalog.list_recipes()`,
`kickoff_lint`'s `fix` field, `concord send|ack|stop|admit`, `board merge|discard`,
`intake once --source`. Its most important affordances — starting a run, answering a
blocked run, deciding a race — are rendered as *chat handoffs* (`S.thread("…")`)
because the page has no verb to call.

**User value.** An engineer owning a mini-ork project can start a run from a saved
kickoff, answer a run that is blocked on them, decide which of four racing runs to
keep, and see which agent holds a hot-file claim — **all without leaving the page and
without opening a terminal.** Today every one of those is a bounce into a chat thread
or a shell.

## Architecture Decision (locked)

1. **The page is JSON composed in the engine; the renderer is not touched.**
   `mini_ork/ide_pages/orch.py` emits sections; `crates/mini_ork_ui` draws all 13
   pages from the same vocabulary (`spec.py`). This feature adds **no section type**
   and **no renderer change**. Anything that would need one is out of scope (§ Out of
   Scope) and is listed there with its cost.
2. **The picker idiom is `chips` + `S.set_args(...)`.** The renderer has no dropdown.
   A "picker" is a `chips` section where each chip re-fetches the page with a new
   `--arg`; the engine reads `args.get(key)` and draws the next state. This is a
   reload-driven picker and is already used elsewhere in the product.
3. **The only act channel is a CLI argv list.** `S.cli(...)` builds argv; there is no
   HTTP POST in the action vocabulary. Therefore **every write this feature needs is
   specified as a `board` subverb**, wrapping the Python function that already
   implements it. This is why `board start` exists rather than a page-side HTTP call.
4. **Two tiers, both required (repo rule).**
   - **Simple tier** — `mini-ork board <verb>` with no `--home`: resolves
     `_default_home()`, works on a laptop with no server and no daemon.
   - **Secure tier** — an explicit `--home` / `MINI_ORK_HOME`, scoped per project,
     used by the IDE (which reads every project with `--home` while the process's own
     home is the app's — see the note at `board_cmd.build_payload`).
   Both tiers reach the same code. § Non-Functional Requirements / Security specifies
   the fail-closed property that makes the second tier safe.

## Phasing Map

| Phase | Contents | Journeys | Risk |
|---|---|---|---|
| **P0 — Wiring** | No new verbs, no schema. Six payload fixes composing existing data. | J3, J4, J7, J8, J10 | Low |
| **P1 — Dispatch** | `board start`, `kickoff new`, the Dispatch tab. | J1, J3 | Medium — spawns processes |
| **P2 — Answers** | Recent-run profile scan, `board answer`, the blocked-on-you panel. | J5, J9 | Low |
| **P3 — Races** | `board race-keep`, `intake_events.run_id`, arrivals link. | J2, J6 | Medium — schema + git merge |
| **P4 — Attention** | The strip, once its four inputs exist. | all | Low |

Each phase is independently shippable and independently revertable. P0 delivers
user-visible improvement with **zero new CLI surface** — it is the recommended first
landing.

---

## Functional Requirements

EARS syntax. `the system` = the mini-ork engine (CLI + `ide_pages` payload).

### Phase 0 — Wiring

#### FR-001: Page errors are rendered, not swallowed
`board_cmd.build_payload` merges the run-reaper's failure into `payload["errors"]`
(verified: `build_payload`, the `reap_errors` merge at the end of the function), and
`orch.build(home, tab, args)` accumulates its own `errors` dict. **Nothing in
`crates/mini_ork_ui/src/` reads the page-level `errors` map** — reproduced: with a
home whose `task_runs` table is unreadable, `errors: {"reaper": "OperationalError: no
such table: task_runs"}` is present on all six tabs and invisible in the UI.

- **When** a page payload carries a non-empty `errors` mapping, **the engine shall**
  append a `callout` section naming each key and its message, with `tone="warn"`.
- **The engine shall** place that section last in every tab's section list, so its
  position does not depend on which tab is open.
- **When** `errors` is empty or absent, **the engine shall** emit no such section
  (no empty box).

#### FR-002: Kickoff rows carry the fix and the log
`kickoff_lint.lint(markdown, *, project, recipe=None, home=None)` returns
`list[{"sev","msg","fix"}]` (verified). `orch._kickoffs_section` renders only
`errors[0]["msg"]` and discards `fix`.

- **The engine shall** render the first finding as `{msg} — {fix}` in a kickoff row's
  `sub`, and shall render the count of remaining findings.
- **Where** a kickoff has a resolved run, **the engine shall** include the run's
  launch log path in the row, so the UI can open it with `S.open_path`.
- **When** a kickoff has never run, **the engine shall** omit the log action rather
  than emit a dead control.

#### FR-003: Thread defaults show their provenance and are settable
`orch._thread_defaults` renders a read-only `kv` whose `sub` prints the env var name
as an apology. `acp_orchestrator.config.orchestrator_lanes(root=None)` returns the
lane options ordered opus → sonnet → remainder (verified); `recipes_catalog.list_recipes(home)`
returns the recipe options; workspace has two values.

- **The engine shall** render the three defaults as `chips` sections, with the active
  value marked and each alternative clickable via `S.set_args(key=value)`.
- **When** a chip is clicked, **the engine shall** apply the value through
  `S.cli("features","set-env","<NAME>","<value>", home=False)` — `home=False` because
  `features` is the one verb that rejects `--home`, matching the existing use in
  `ide_pages/setup.py`.
- **The engine shall** label each default with the layer it is read from
  (`env` · `features.yaml` · lane map · built-in default), resolved by reading the
  same precedence `default_lane(home)` applies.
- **The note shall** state that a value set here overrides the lane map, because
  `default_lane` tests `MO_ORCHESTRATOR_LANE` first.

#### FR-004: History thread rows are actionable
`orch._history`'s note claims *"Opening one replays it; the next prompt continues the
same conversation"* — and the thread rows carry `{"cells": [...]}` with **no `"do"`**.
Only run rows have one. The note is presently false.

- **The engine shall** give each thread row a `do` that opens the thread page
  (`mini_ork/ide_pages/thread.py`), passing whatever argument that module keys on.
- **Where** a row is a run rather than a thread, **the engine shall** keep its
  existing run-opening action.
- **The engine shall** mark the two kinds distinctly in the row, so the two targets
  are distinguishable without reading the `sub` text.

#### FR-005: Fleet rows are actionable
`orch._coord` renders principals, hot-file claims and `--owns` violations as three
tables with **no actions**. The verbs live under `mini-ork concord`
(`run|ps|stop|send|inbox|ack|claims|violations|admit`) — reached through
`mini_ork.orchestration.concord`, which `main.py` registers as `concord`. `concord`
takes no `--home`, so every action here is `home=False` and rides the `MINI_ORK_HOME`
the IDE sets on the spawn.

- **The engine shall** attach `acts` to each principal row: inbox, send, stop.
  - Inbox is `S.cli("concord", "inbox", "--principal", pid, home=False)`.
  - Send is an `S.thread("/concord send <pid> ")` prefill, **not** an `S.cli` action:
    `concord send` takes a required `message` (`nargs="+"`), which a click cannot
    supply, so a `cli` action would be a button that always fails to parse.
  - Stop is `S.cli("concord", "stop", pid, "--yes", confirm=…)`. `--yes` is required
    because the IDE spawns with no tty, where the verb otherwise refuses; the gate
    the operator sees is the action's own `confirm`.
- **The engine shall** attach to each claim row the path it claims (open, absolute
  paths only) and a stop-the-holder action carrying a `confirm`.
  *Correction:* the original draft asked for `concord admit` here. `admit` is scoped
  to an epic whose plan overlaps one already in progress and takes no principal — it
  is not a per-claim act. No admit button is rendered.
- **The engine shall** attach the same open-path and stop-the-holder acts to each
  violation row, keyed on `worktree_principal` — the principal that made the edit.
- **The engine shall** name the run a principal belongs to **only** when the payload
  names one: `cn_client.extract_run_id(principal)` accepts the canonical
  `run-<digits>-<short>` shape and returns `""` otherwise, so the link is absent
  rather than wrong. `coord_list_principals` carries no run id by contract, so in
  practice this renders no link today — it is the hook the P4 attention work needs,
  not a fix that changes what is on screen.

#### FR-006: Intake chain health is a checklist
`orch._intake` already computes source registration, trigger and skip reason; the
page renders them as a `list` plus a prose callout whose remedy is a sentence.

- **The engine shall** emit an intake chain-health section of type `checks`, with one
  row per link: source registered → trigger configured → token present → scheduler
  running → reaper healthy, each carrying state, a detail, and the CLI that repairs
  it.
- **The engine shall** populate that section's `passing`/`failing`/`pending` summary
  from the same rows, so the header count cannot disagree with the rows.
- **When** the scheduler or reaper state is unreadable, **the engine shall** render
  the row as `failing` with the error text as its detail — never as `passing`.

### Phase 1 — Dispatch

#### FR-010: `board start` — launch a run detached
**The single most important requirement in this document.** The ability to start a
run exists (`web.control.launch_run`, verified: validates recipe/run-id against a safe
token charset, stages the kickoff at `<home>/runs-inbox/<run_id>.md`, spawns
`bin/mini-ork run <recipe> <kickoff>` with `start_new_session=True`, returns
`{ok, run_id, recipe, pid, blocked_premium, kickoff_path, log_path}`). It has 12
callers, all internal — HTTP routes, the ACP agent and `threads/service` — and **no
CLI verb**. The IDE therefore cannot start a run; it can only open a chat.

- **The system shall** add `start` to the `board` verb choices and implement
  `mini-ork board start --kickoff <path> [--recipe R] [--workspace worktree|in-place]
  [--set K=V ...] [--run-id ID] [--dry-run]`.
- **The flag names `--set` and `--workspace` shall** match `mini-ork run`'s existing
  flags exactly, since `board start` is that same launch performed detached. `--set`
  maps to `launch_run`'s `extra_env`, and inherits its premium gating
  (`gate_env`) unchanged.
- **When** `--recipe` is omitted, **the system shall** resolve the recipe from the
  kickoff's front matter, using the same resolution the kickoff rows already display,
  and shall record which source won.
- **When** `--dry-run` is given, **the system shall** resolve everything, report the
  recipe, workspace, home, run id and the kickoff path it *would* write, and shall
  spawn nothing.
- **The system shall** print the `launch_run` result as JSON on stdout, and **shall**
  exit non-zero when `launch_run` returns `ok: False`.
- **When** `--home` names a directory that is not an initialised mini-ork home,
  **the system shall** refuse with a clear error and shall create nothing, unless
  `--init` is passed. *(Rationale: `launch_run` calls
  `inbox.mkdir(parents=True, exist_ok=True)`, so a typo'd `--home` silently
  materialises a home and starts a run against it. This is the fail-closed property
  § Security requires.)*
- **The system shall not** pass any user-supplied value through a shell; the argv
  list and the safe-token check in `launch_run` are the existing guarantee and are
  preserved.
- **When** `--kickoff` names a path outside the home's kickoff directories, **the
  system shall** still accept it (an engineer may legitimately start a kickoff from
  anywhere) but **shall** echo the resolved absolute path in the result.

#### FR-011: `kickoff new` — draft a kickoff file
No authoring path exists; `Kickoffs → New kickoff` opens a chat with the literal
string `"/kickoff "` — trailing space, no argument.

- **The system shall** add a `kickoff` top-level verb with a `new` subverb:
  `mini-ork kickoff new <text>`.
- **The system shall** write `.mini-ork/kickoffs/<slug>.md` where `<slug>` is
  derived from `<text>`, seeding front matter with the resolved default recipe and a
  `## Success` stub — so a fresh draft lints closer to clean than to red.
- **When** the target file already exists, **the system shall** refuse rather than
  overwrite, and **shall** print the existing path so the caller can open it.
- **The system shall** print the written path on stdout.

#### FR-012: Dispatch tab composition
- **The engine shall** render a `dispatch` tab whose sections are, in order: the
  recipe/workspace override chips (FR-003's mechanism, page-scoped), the kickoff list
  with per-row actions (FR-002), the new-kickoff composer (FR-011), and the lint panel
  for the row selected by `args["kickoff"]`.
- **The engine shall** render each kickoff row's primary action as
  `S.cli("board","start","--kickoff",<abs path>,"--recipe",<selected>,"--workspace",<selected>)`,
  with `kind="primary"` and **no `confirm`** — starting a run is the purpose of the
  page; confirming it would be parody.
- **When** a kickoff's lint reports an error-severity finding, **the engine shall**
  still render the start action and **shall** mark the row so the risk is visible —
  a lint error is advice, not a gate. (The existing lint verdict already gates
  elsewhere; this page must not silently become a second, invisible gate.)

### Phase 2 — Answers

#### FR-020: Recent-run profile scan
`orch._planner` inspects only the newest run that holds a `run_profile.json`
(verified: `_latest_run_with`). A run parked on `needs_answers` three runs back is
invisible.

- **The engine shall** provide a helper returning the profiles of the most recent N
  runs (N=60), reading each run directory's `run_profile.json` through the existing
  reader.
- **When** a run directory has no profile, **the engine shall** skip it silently —
  a run without a profile has not asked a question.
- **When** a profile cannot be parsed, **the engine shall** skip the run and record
  the parse failure in the page `errors` map (which FR-001 then surfaces).
- **The engine shall** order results so runs waiting longest appear first.

#### FR-021: `board answer` — answer a blocked run
The writer exists: `web.control.save_answers(home, task_run_id, answers) -> {ok,
task_run_id, answers_saved, answers_path, profile_path, profile_status, next_step_cli,
note}` (verified) — it persists `profile-answers.json`, merges into `run_profile.json`,
flips `profile_status` to `ready`, raises `confidence` to ≥0.9 and clears
`human_questions`. It is reachable over HTTP only.

- **The system shall** add `board answer <run_id> --text <T>` reusing the parser's
  existing `--text` argument (already defined for `steer`) and requiring it.
- **The system shall** map a single `--text` answer onto the run's pending questions:
  when the run has one outstanding question, the text answers it; when it has several,
  the system **shall** refuse and **shall** list the question ids, so an answer is
  never silently attached to the wrong question.
- **When** the answer succeeds, **the system shall** return `next_step_cli` unchanged
  from `save_answers`, so the page can offer a follow-up start action.
- **When** the run has no pending questions, **the system shall** refuse with a reason
  rather than writing an orphan answer file.
- **The system shall** accept repeated `--for <question-id> --text <T>` pairs to
  answer several questions in one call. *(Extension; the single-question form is the
  minimum.)*

#### FR-022: Blocked-on-you panel
- **The engine shall** render, on the Decide tab, one `list` entry per run with
  `needs_answers` true (using `get_profile`'s `needs_answers` field, which is
  `profile_status == "needs_answers" and len(questions) > len(answers)`).
- **The engine shall** give each entry its own `composer` bound to
  `S.cli("board","answer",<run_id>,"--text")`, so the typed text is the answer.
- **The engine shall** render the wait duration per entry, and **shall** escalate the
  entry's tone with age.
- **When** a run has been waiting beyond a configurable threshold, **the engine shall**
  include the run's recipe and start time so the cost of leaving it parked is visible.

### Phase 3 — Races and arrivals

#### FR-030: `board race-keep` — decide a race in one call
Races are grouped by the page (`orch._races`, a 180 s window on the same kickoff).
`workspaces.merge(ws, message, *, approver=None)` and `workspaces.discard(ws, *,
approver=None)` both take a resolved `Workspace`, and `board_cmd.act` resolves it with
`workspaces.load(home, run_id)` (all verified). The IDE action vocabulary cannot chain
`merge && discard && discard`, so "keep one, drop the rest" is currently 4 clicks.

- **The system shall** add `board race-keep <winner_run_id> [--discard RUN_ID ...]
  [--dry-run] [--force]`.
- **When** `--dry-run` is given, **the system shall** return the plan — the winner, the
  runs to discard, and the worktrees that would be freed — and **shall** perform no
  merge and no discard.
- **The system shall** discard only runs whose kickoff matches the winner's, and
  **shall** refuse a `--discard` naming a run from a different kickoff. *(Safety: the
  page supplies the sibling ids it displayed; the verb must not trust them blindly.)*
- **When** a discard fails, **the system shall** continue with the remaining runs and
  **shall** report per-run outcomes, so a partial failure is stated rather than
  masked. The overall result **shall** be `ok: False` when any step failed, and
  **shall** name which step.
- **The system shall** record the operator identity as `approver`, using the same
  `ide:<user>` fallback `act` already applies.
- **When** `--force` is absent and the review policy requires approval, **the system
  shall** propagate the merge gate's refusal (`needs_approval`) rather than bypassing
  it.

#### FR-031: The intake ledger records which run an arrival started
`intake_events` has no `run_id` (verified schema: `id, provider, delivery_id,
payload_hash, issue_uid, thread_id, action, detail, created_at`). `intake/dispatch.py`
calls `store.record_intake_event(...)` with `thread_id`. A thread can start several
runs, so the thread is not a proxy for the run.

- **The system shall** add a nullable `run_id TEXT` column to `intake_events`.
- **The system shall** add the column on **both** schema paths: to `SCHEMA_SQL` in
  `mini_ork/threads/store.py` (fresh databases) **and** as migration
  `db/migrations/0074_intake_events_run_id.sql` (existing databases).
- **Because** `ensure_schema` runs only `CREATE TABLE IF NOT EXISTS` statements and
  SQLite's `ALTER TABLE` has no `IF NOT EXISTS`, **the system shall** guard the
  upgrade with a `PRAGMA table_info` check before the `ALTER`, following the existing
  precedent (`_ensure_body_columns` in `mini_ork/pm/store.py` and `_ensure_column` in
  `mini_ork/stores/migrate.py`). *A bare `ALTER` in the migration will hard-fail on a
  database that already ran it; the guard is not optional.*
- **The system shall** keep `SCHEMA_SQL` and the migration text in agreement, as the
  existing test convention for this store requires.
- **`record_intake_event` shall** accept an optional `run_id` and persist it, defaulting
  to `None` so existing callers are unaffected.
- **`intake/dispatch.py` shall** pass the run id when it has one, from the point where
  the run is actually started.

#### FR-032: Arrivals link to their runs; old rows degrade
- **The engine shall** render the arrivals ledger's run cell as an openable run when
  `run_id` is present.
- **When** `run_id` is NULL — every row written before this feature — **the engine
  shall** render an em dash, not an error and not an empty action.
- **The engine shall** render a per-source sync action using the existing
  `intake once --source <S>` verb, so a source can be polled on demand.

#### FR-033: Race decision panel
- **The engine shall** render one entry per race, not one table row per race, because
  the contestants are the decision input and belong on screen beside it.
- **The engine shall** show per-contestant: lane, run id, verification outcome, check
  counts and cost, and an action opening that run.
- **The engine shall** render each "keep" action as `S.cli("board","race-keep",<winner>,
  "--discard",<sibling>,...)` carrying a `confirm` that names the run being kept, the
  runs being discarded, and the worktrees freed — populated from a `--dry-run` result
  so the numbers are real, not estimated by the page.

### Phase 4 — Attention

#### FR-040: The attention strip
- **The engine shall** prepend a single `triage` section to the section list of
  **every** tab, composed from four inputs: runs waiting for an answer (FR-020),
  undecided races (FR-033's grouping), intake chain failures (FR-006), and page
  errors (FR-001).
- **When** all four inputs are empty, **the engine shall** emit no strip — not an empty
  one.
- **The engine shall** render each strip item's action as the CLI or page action that
  resolves it, so the strip is a to-do list, not a status board.
- **The engine shall** render the strip's counts from the same values the tabs render,
  so the strip and the tab cannot disagree.

---

## Non-Functional Requirements

### Performance
- **The page shall** keep its existing warm budget. `board_cmd` documents a 1.5 s warm
  target for the board payload and excludes five sections to hold it; the orchestrator
  page's additions shall not add a blocking network call to the default tab.
- **The recent-run scan (FR-020) shall** read at most 60 run directories and **shall**
  stop reading a run's profile at the first parse failure rather than retrying.
- **`board start` shall** return as soon as the child is spawned — `launch_run` returns
  immediately with a pid and never waits for the run. The verb shall not poll.
- **`board race-keep` shall** run its git operations sequentially and **shall not** hold
  a lock across a merge that requires human approval.

### Security
- **`board start` shall fail closed on an uninitialised `--home`** (FR-010), because
  `launch_run` creates the directory tree it is given. A typo must not produce a run.
- **The system shall not** introduce a path where a user-supplied value reaches a shell.
  `launch_run` uses an argv list and validates recipe and run id against
  `_SAFE_TOKEN_CHARS`, which excludes `/` and `..`; that guarantee shall be preserved.
- **`board race-keep` shall not** discard a run whose kickoff does not match the winner's
  (FR-030), because discard is destructive and the sibling list comes from the caller.
- **The system shall not** write a credential to stdout, to a log line, or into the
  `runs-inbox` staging file. Values passed via `--set` go to the child's environment
  only, and `gate_env` shall be applied before they are merged, exactly as `launch_run`
  does today.

### Scalability / Limits
- **`board answer` shall** refuse a multi-question answer it cannot disambiguate
  (FR-021) rather than writing a partial map.
- **The arrivals ledger shall** remain bounded by its existing limit clause; the
  `run_id` column shall not change the query's shape.
- **The attention strip shall** cap each item class at a small count and render a
  remainder line, so a project with 200 parked runs does not produce a 200-line strip.

### Compatibility / Rollback
- **The system shall not** change the page payload's `version` field: every addition is
  a new optional key. An older IDE renderer shall render the unchanged sections and
  ignore the new ones.
- **The system shall not** change any existing `board` verb's payload shape. The four
  new verbs and one new column are additive.
- **`record_intake_event`'s new parameter shall** be keyword-only with a `None` default,
  so every existing caller keeps its current behaviour.
- **Rolling back** the migration is not supported (SQLite column drops); the column is
  nullable and ignored by older readers, so rollback is "stop writing it".

---

## Acceptance Criteria

Each maps to one of the ten engineer journeys.

### AC-001: J1 — Start a run from a saved kickoff
**Given** an initialised home with a lint-clean kickoff `fix-auth.md`,
**When** the engineer runs `mini-ork board start --kickoff .mini-ork/kickoffs/fix-auth.md --recipe code-fix --workspace worktree`,
**Then** the command exits 0 within one second, prints a run id (`run-<epoch>-<hex>`), the recipe, a pid and a log path, **and** the run appears in `mini-ork board show` without any further action.

### AC-002: J2 — A ticket's run is linked
**Given** an intake ticket that starts a run,
**When** the engineer opens the Intake tab,
**Then** the arrivals row for that ticket shows the run id as an openable action, and the row's action reads `launched`.

### AC-003: J3 — Draft, lint, run
**Given** no saved kickoff matches the task,
**When** the engineer types a sentence into the new-kickoff composer and submits,
**Then** a file is written under `.mini-ork/kickoffs/`, its path is printed, the dispatch list shows it with its lint findings (each with its `fix`), and the row's start action is present.

### AC-004: J4 — A broken chain is named
**Given** a home whose run-reaper fails because the state database is unreadable,
**When** any Orchestrator tab is opened,
**Then** a warning callout naming `reaper` and its error message is rendered on that tab.

*Correction:* the original draft also required "the intake chain-health checklist
shows the reaper row as `failing`". The checklist has **no reaper row by design**:
the reaper runs before the page is built (its error is already on the page via the
FR-001 callout), and reaping again to test it would be a second write on a read path.
A callout that says the same finding once is the whole requirement.

### AC-005: J5 — Answer a blocked run
**Given** a run whose profile status is `needs_answers` with one outstanding question,
**When** the engineer submits an answer from the blocked-on-you panel,
**Then** `profile-answers.json` contains the answer, `run_profile.json`'s status reads `ready`, its `human_questions` is empty, and the command returns a `next_step_cli`.

### AC-006: J6 — Decide a race
**Given** four runs sharing one kickoff, all with open worktrees,
**When** the engineer confirms the keep action for one of them,
**Then** exactly that run's branch is merged, the other three worktrees are discarded, and the result reports per-run outcomes — **and** when one discard fails, the result is `ok: False` and names the failed step while the other discards still complete.

### AC-007: J7 — Change the orchestrator lane
**Given** the defaults read `opus`,
**When** the engineer clicks the `sonnet` chip,
**Then** `.mini-ork/config/features.yaml` records `MO_ORCHESTRATOR_LANE: sonnet`, the reloaded page shows `sonnet` active with its provenance layer named, and a subsequently created thread defaults to that lane.

### AC-008: J8 — Resolve a coordination problem
**Given** a live principal holding a hot-file claim,
**When** the engineer uses that row's stop action,
**Then** the corresponding `concord stop` runs, and the page reports the outcome; and when ContextNest is unreachable, the tab renders one explanation and no actions rather than empty tables.

### AC-009: J9 — Review classifications
**Given** twelve recent runs with profiles, two of them below confidence 0.7,
**When** the engineer opens the classifications table,
**Then** all twelve appear ordered so the low-confidence rows are visible, and selecting a row shows **that** run's plan — not the newest run's.

### AC-010: J10 — Resume a thread
**Given** a prior orchestrator thread,
**When** the engineer activates its row,
**Then** the thread page opens for that thread; the row's kind is distinguishable from a run row before clicking.

### AC-011: Two tiers
**Given** a laptop with no server,
**When** `mini-ork board start --kickoff <path>` is run with no `--home`,
**Then** it starts against the default home and succeeds; **and** when `--home` names a directory that is not an initialised mini-ork home, the command refuses with a clear error and creates no directory.

### AC-012: Renderer untouched
**Given** the implementation of all phases,
**When** the changed files are listed,
**Then** none is under `crates/mini_ork_ui/` — the page payload changes only, and an unmodified IDE renders every existing section.

---

## Error Handling

| Condition | Behaviour | Exit / payload |
|---|---|---|
| `board start` with an uninitialised `--home` | Refuse; create nothing; name the path | rc 2, `{"ok": false, "error": ...}` |
| `board start` with a kickoff path that does not exist | Refuse before spawning | rc 2 |
| `board start` with no `--recipe` and no front-matter recipe | Refuse; name both places it looked | rc 2 |
| `launch_run` returns `ok: False` | Propagate its `error` verbatim to stderr and stdout | rc 1 |
| `board answer` on a run with no pending questions | Refuse with a reason; write nothing | rc 1, `{"ok": false, "reason": ...}` |
| `board answer` on a run with multiple pending questions and one `--text` | Refuse; list the question ids | rc 2 |
| `board race-keep` with a `--discard` from a different kickoff | Refuse that discard; name both kickoffs | `ok: false`, per-run outcomes |
| `board race-keep` where the merge gate requires approval | Do not discard; propagate `needs_approval` | `ok: false`, `needs_approval: true` |
| A run profile that cannot be parsed | Skip the run; add the parse failure to `payload["errors"]` | page renders it via FR-001 |
| `errors` empty on a page | No callout section | unchanged payload |
| `intake_events.run_id` is NULL | Render an em dash | unchanged payload shape |
| Scheduler or reaper state unreadable | Chain-health row reads `failing` with the error as detail | never `passing` |

---

## Out of Scope

Stated explicitly, with cost, so a reviewer can decline them knowingly.

| Item | Why out | Cost if wanted |
|---|---|---|
| A `select`/`dropdown` section type | Would replace chips + reload with real pickers (J1, J7, J9) | Touches all 13 pages; needs its own spec + design pass |
| Rendering the page-level `errors` map **in the renderer** | FR-001 solves it engine-side | Defence in depth only; same 13-page blast radius |
| Keyboard-navigable tab strips | Accessibility baseline, genuinely missing | Renderer change, all pages |
| Row expansion inside `list` | Inline lint detail per kickoff | Renderer change, all pages |
| The orchestrator **thread's** chat panel, model picker, steering | Zed's agent panel owns it | Different surface entirely |
| Other IDE pages | Cross-page promotion of blocked runs to the `inbox` page is a product call | Separate spec |
| `J9`'s router-correction path (`prefs set --scope task_class`) | **Deliberately excluded** — it writes durable routing behaviour for *future* runs, which is a product decision, not a UI one. J9 ships read-only. | Small BE (the verb exists); the question is whether the page should own it |
| Recipe and lane **definition** | This feature changes how they are *chosen*, not defined | — |
| Auto-resume of an answered run | `save_answers` returns `next_step_cli` and its own note says future versions will auto-resume | Real feature; needs its own spec |

---

## Implementation TODO

### P0 — Wiring (no new verbs, no schema) — **implemented 2026-10-15, not yet tested**
- [x] FR-001 — `spec.errors_callout(errors)`; appended in `board_cmd.build_payload`, not
      `orch.build`, because the reaper merges its error into `payload["errors"]` *after*
      `build_page` returns — a callout built inside `orch.build` would miss it.
- [x] FR-002 — `orch._finding_text` + `orch._run_log`; kickoff rows render
      `{glyph} {msg} — {fix}` with a total count, and carry a `Log` button only when
      `runs/<id>/execute.log` or `runs-inbox/<id>.launch.log` exists.
- [x] FR-003 — `orch._env_source` / `_lane_source` / `_picker` / `_thread_defaults`:
      three `chips` groups; writes via `features set-env … home=False`; provenance per
      value. The live loop is closed: `apply_defaults(home)` at `board_cmd.py:1119`
      reads `<home>/config/features.yaml` back into `os.environ` on the next render.
- [x] FR-004 — `orch._history`: thread rows carry `S.page_link("thread", thread=…)`,
      kind glyphs `▤ thread` / `◇ run`, and the note now says the true thing.
- [x] FR-005 — `orch._coord`: `acts` on principals, claims and violations, via
      `_concord_stop` (confirm + `--yes`) and `_open_abs` (absolute paths only). No
      `admit` — see the FR-005 correction.
- [x] FR-006 — `orch._chain_health`: per-source registration, trigger and last-sync
      rows plus a Scheduler row; emitted at IDE spec level ≥ 2, with the old callout
      kept for level 1.
- [ ] Tests: a unit test per section, asserting the new keys exist and that a missing
      source degrades rather than raising. **Not written** — held per the working
      agreement that test work starts after the implementer confirms.
- [ ] **Add `"orch": ("session", "kickoffs", "races", "planner", "coord", "intake")`
      to `tests/unit/test_ide_pages_actions.py::_PAGE_TABS`.** It lists only `intake`
      today, so the `cli` actions FR-003 and FR-005 add are outside the argv walk. They
      were verified by hand (all 6 tabs build; every emitted `cli` action parses against
      its own parser) and by the 20 tests in `test_ide_pages_actions.py` +
      `test_cn_ui_coordination.py`, which pass — but a future malformed action on those
      two tabs would ship unseen, which is the exact gap the `kanban` comment in that
      file describes.

### P1 — Dispatch
- [x] FR-010 — `board start`: add `start` to `build_parser().choices`; add `--kickoff`, `--set` (append), `--run-id`, `--workspace`, `--init`, `--dry-run`; exempt `start` from `validate`'s run-id rule and require `--kickoff`; implement `_act_start` wrapping `web.control.launch_run`; fail closed on an uninitialised home.
      Verified by hand (10 probes, `.venv/bin/python`): no `--kickoff` → rc 2; **uninitialised home → rc 2 and no `runs-inbox` created** (fail-closed); `--init` proceeds; recipe resolves from front matter → `--recipe` → refusal; `--set X=1`/`--workspace` map into `extra_env`; a stubbed `launch_run` receives `(home, recipe, text, run_id, extra_env)` and a kickoff body; `launch_run` returning `ok:False` → rc 1 with the error on stderr. A **live spawn was not run** — starting a real run is a side effect on the shared engine, so `launch_run` was stubbed instead; AC-001's live replay needs a runner.
- [x] FR-011 — new `mini-ork kickoff new <text>`: `mini_ork/cli/kickoff_cmd.py`, registered in `main.py`'s `_NATIVE_MODULE_SUBS` as `"kickoff"`. Template writer via `kickoff_lint.slug`; seeds front matter with `--recipe` → `MO_ACP_RECIPE` → `code-fix`; `## Success` stub; refuses to overwrite and prints the existing path; prints the written path on stdout.
      Verified by hand: drafted `fix-the-auth-middleware-timeout.md` lints **0 findings**; `MO_ACP_RECIPE=refactor` seeds `recipe: refactor`; `board start --dry-run` on a draft reads back `recipe=code-fix source=front matter` — the write→read loop closes. Overwrite → rc 2; empty goal → rc 2; no subverb → usage, rc 2.
- [x] FR-012 — `orch`: the `dispatch` tab and its row actions. TABS gained `("dispatch", "Dispatch")` (second). Sections in FR-012's order: page-scoped recipe/workspace chips (`S.set_args`), the kickoff list, the `composer` bound to `S.cli("...", ["kickoff","new"])`, the lint panel for `args["kickoff"]`. `_kickoffs_section` was refactored onto a shared `_scan_kickoffs`/`_lint_badge`/`_kickoff_parts` so the two tabs cannot disagree.
      Verified by hand: all 7 tabs build at **both** IDE spec levels with no `errors`; the lint panel appears only when a kickoff is selected (checks at level 2, markdown fallback at level 1); a walked **23 `cli` actions parse against their own parsers, 0 failures** (`features` rejects `--home` and every such action carries `home=False`); AC-003's chain reproduced end to end — composer writes the file, the dispatch list shows it, its Start action is present, selecting it renders the lint panel. A live spawn: see FR-010.
- [ ] Tests: parser round-trip (mirroring `tests/unit/test_board_cmd.py`'s guard), dry-run writes nothing, uninitialised home refuses, and a real spawn is asserted from `launch_run`'s return rather than by waiting for the run. **Not written** — held per the working agreement.

### P2 — Answers
- [ ] FR-020 — the recent-run profile scan, shared with P3's classification table.
- [ ] FR-021 — `board answer <run_id> --text T` reusing the parser's existing `--text`; single-question enforcement; passthrough of `next_step_cli`.
- [ ] FR-022 — the blocked-on-you panel with per-question composers.
- [ ] Tests: answering a single-question run flips the status; a multi-question run refuses; a run with no questions refuses.

### P3 — Races and arrivals
- [ ] FR-031 — `run_id` column: `SCHEMA_SQL` + `db/migrations/0074_intake_events_run_id.sql` with a `PRAGMA table_info` guard; extend `record_intake_event`; wire `intake/dispatch.py`; keep the schema/migration agreement test green.
- [ ] FR-030 — `board race-keep`: resolve both ends through `workspaces.load`, verify kickoffs match, dry-run plan, per-run outcome reporting, `needs_approval` passthrough.
- [ ] FR-032 — arrivals run link + per-source sync via `intake once --source`.
- [ ] FR-033 — race decision panel; `confirm` populated from a `--dry-run`.
- [ ] Tests: migration idempotency (run it twice), a NULL `run_id` row renders an em dash, a cross-kickoff discard is refused, a partial discard failure reports honestly.

### P4 — Attention
- [ ] FR-040 — the `triage` strip prepended to every tab; omitted when empty; counts shared with the tabs.
- [ ] Tests: strip absent when all inputs are empty; counts equal the tab counts.

### Cross-cutting
- [ ] NFR — warm-page budget held on the default tab (measure, do not assume).
- [ ] NFR — confirm no changed file lies under `crates/mini_ork_ui/` (AC-012).
- [ ] Live replay for the ACs of each shipped phase, per the repo's gate-every-run-by-hand habit.

---

## Renderer conformance (`zed-mini-ork` → `crates/mini_ork_ui`)

A separate audit was run against the fork's renderer (the `mini_ork_ui` submodule)
because the engine can emit a key the fork never reads, and the renderer **defaults
on absence** (`str_of`/`arr`/`bool_of`) — so the drift is always a *silently dropped
control*, never a blank section. Nothing below blocks any section from rendering;
each is data the engine sends and the fork discards.

**Matches (no action):** `chips` (`t`/`on`/`do`), `list` (`acts` **is** rendered,
`page.rs:1142-1158`), `table` (dict row form, `do`/`sel`/`acts`/`cells`), `checks`
**state strings** (`na` is the `_` catch-all — `sections.rs:1121`), `composer`, `files`,
`agents` row shape, `hero`, `triage`.

**Gaps found, ranked by whether this spec's UX depends on them:**

| # | Key the engine emits | Dropped by | Consequence | Fix |
|---|---|---|---|---|
| 1 | a **list-act's `kind`** | `page.rs:1151` hard-codes `"ghost"` | FR-012's Start action is styled ghost, not primary — the tab's whole point reads as secondary | honour `b.get("kind")`, default `ghost` |
| 2 | `check_row["do"]` | `render_checks_body_inner` never calls `action_handler` on a row | a per-check action (`verify.py:105` passes `open_path`) is dead | draw a row button when `do` is present |
| 3 | `checks.summary["na"]` | `sections.rs:1076-1078` reads only passing/failing/pending | a checks section with `na` rows misreports its own header | render the `na` count |
| 4 | `columns` section `title`/`note`/`actions`, card `id` | `render_columns` reads only `cols` (`sections.rs:339`) | `kanban.py:601` passes a `note` nobody sees | draw the generic card header for columns |
| 5 | `story_step` `kind`/`model`/`meta`/`open`, section `title` | `render_story` reads neither | `run_story.py:432` passes `"What happened"` — invisible | draw the section title; surface `meta` |
| 6 | `finding["source"]` | `render_findings_body_inner` reads 6 of 7 | minor | draw or drop the key engine-side |
| 7 | **`tone="warn"`** (`cn_file.py:554`) | not a colour in `palette.rs:200-214` | a callout silently loses its colour and mark — **an emitter bug, not a renderer gap** | change the emitter to `orange` |

**AC-012 tension, resolved.** AC-012 ("no changed file under `crates/mini_ork_ui/`")
was scoped to *the redesign*: its sections must need no new renderer work, and they
don't — the locked architecture decision holds. The gaps above are **pre-existing
engine↔fork drift**, not redesign work, and the instruction that authorised this pass
was explicit that the fork must render what the engine emits. Rows 1-3 are fixed in
the fork; rows 4-6 and the row-7 emitter bug are tracked here.
Owner of a fork fix: the `mini_ork_ui` submodule repo — commit there, pin the
submodule in `zed-mini-ork`, and push `fork`, never `origin`.

---

## Appendix — Verified code anchors

Every claim above was read from disk in the engine repo (`SourceShift/mini-ork`,
`main`) during this spec's authoring. Cite these by symbol, not line.

| Symbol | File | What it establishes |
|---|---|---|
| `launch_run` | `mini_ork/web/control.py` | The detached launch `board start` wraps; returns `{ok, run_id, recipe, pid, blocked_premium, kickoff_path, log_path}`; validates via `_is_safe_token`; `inbox.mkdir(parents=True, exist_ok=True)` — the fail-closed rationale in FR-010 |
| `_SAFE_TOKEN_CHARS`, `_is_safe_token` | `mini_ork/web/control.py` | The existing no-traversal guarantee |
| `get_profile` | `mini_ork/web/control.py` | Returns `needs_answers` — the Decide tab's input |
| `save_answers` | `mini_ork/web/control.py` | The writer `board answer` wraps; returns `next_step_cli` |
| `build_parser`, `validate`, `build_payload`, `main`, `act` | `mini_ork/cli/board_cmd.py` | Where the four verbs slot in; `act` resolves `workspaces.load(home, run_id)` and applies the `ide:<user>` approver fallback |
| `_board_shell_payload` | `mini_ork/cli/board_cmd.py` | The documented 1.5 s warm budget and the five sections excluded to hold it |
| `merge`, `discard` | `mini_ork/workspaces.py` | Both take a resolved `Workspace`; `merge` honours `before_merge: required` and surfaces `needs_approval` |
| `SCHEMA_SQL`, `ensure_schema`, `record_intake_event` | `mini_ork/threads/store.py` | `ensure_schema` runs `CREATE TABLE IF NOT EXISTS` only — **no ALTER support**; `intake_events` has no `run_id` |
| `_ensure_body_columns` | `mini_ork/pm/store.py` | The `PRAGMA table_info` → `ALTER TABLE` precedent, with a matching numbered migration |
| `_ensure_column` | `mini_ork/stores/migrate.py` | The sanctioned guarded-ALTER helper |
| `db/migrations/` | repo root | Highest existing number is `0073`; this feature adds `0074` |
| `orchestrator_lanes` | `mini_ork/acp_orchestrator/config.py` | The lane picker's options, ordered opus → sonnet → remainder |
| `dispatch_issue`, `store.record_intake_event` call site | `mini_ork/intake/dispatch.py` | Where the run id must be threaded through |
| `TABS`, `build`, `_kickoffs_section`, `_history`, `_thread_defaults`, `_races`, `_planner`, `_coord`, `_intake`, `_arrivals`, `_latest_run_with` | `mini_ork/ide_pages/orch.py` | The page under redesign — **791 lines, moved since the companion briefs** |
| CLI verb registry (no `kickoff` entry) | `mini_ork/cli/main.py` | `kickoff new` is a free top-level name; `run`'s `--set`/`--workspace` flags are the vocabulary `board start` mirrors |
