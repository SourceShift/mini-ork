# Feature: Orchestrator page — dispatch, intake, decisions and fleet

> **Companion briefs (zed-mini-ork repo, `docs/ux-briefs/`):**
> `2026-10-15-orchestrator-intake.md` (the audit — what is wrong),
> `2026-10-15-orchestrator-intake-redesign.md` (the journey + FE/BE design).
>
> ⚠️ **The line anchors in both companions are stale.** They were taken against
> `mini_ork/ide_pages/orch.py` at 604 lines; it is now **~930** and the functions
> have moved (`_arrivals` 485→723, `_planner` 312→900, `build` 565→1625). Cite the
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
| **P0 — Wiring** ✅ | No new verbs, no schema. Six payload fixes composing existing data. | J3, J4, J7, J8, J10 | Low |
| **P1 — Dispatch** ✅ | `board start`, `kickoff new`, the Dispatch tab. | J1, J3 | Medium — spawns processes |
| **P2 — Answers** ✅ | Recent-run profile scan, `board answer`, the blocked-on-you panel, the classifications table. | J5, J9 | Low |
| **P3 — Races** ✅ | `board race-keep`, `intake_events.run_id`, arrivals link, the race panel. | J2, J6 | Medium — schema + git merge |
| **P4 — Attention** ✅ | The strip, once its four inputs exist. | all | Low |

✅ = implemented (P0 2026-10-15, P1–P4 2026-10-16, P2's classifications table FR-023 the
same day). All four phases are in, and every acceptance criterion now has something
implementing it.

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
The page had no scan at all: `orch._planner` inspected only the newest run holding a
`run_profile.json` (a `_latest_run_with` helper that has since been deleted — FR-023
replaced its one caller). A run parked on `needs_answers` three runs back was
invisible, and no run's classification could be revisited.

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

#### FR-023: The classifications table
J9's surface. The tab reported one run — whichever was newest — so the router's recent
decisions were unreviewable and a mis-route could not be looked up at all.

- **The engine shall** render, on the Classify & plan tab, one `table` of the runs
  FR-020's scan found, with the run, its task class, its recipe, its confidence and its
  outcome as columns.
- **The engine shall** order that table by confidence, lowest first, so the rows most
  likely to be a mis-route come first.
- **When** a profile records no confidence, **the engine shall** sort it after every run
  that recorded one, rather than as the least confident — a gate-skipping recipe is not
  a doubtful decision.
- **The engine shall** state in the table's note how many of its rows fall below the
  low-confidence threshold.
- **When** a row is activated, **the engine shall** re-read the tab with that row's run
  id (`S.set_args`) and **shall** render that run's classification, plan and open
  questions below the table — not the newest run's.
- **When** no run id is given, **the engine shall** detail the first row drawn, so the
  detail never describes a run the table is not highlighting.
- **When** the given run id names no row, **the engine shall** ignore it: the id arrives
  as a string from the IDE and is never joined into a filesystem path.

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
- **When** the merge is refused, **the system shall** stop before the first discard.
  *(Safety: dropping the runners-up when the winner did not land leaves the race with
  nothing, so "nothing is discarded unless the merge landed" is a rule, not an
  ordering accident.)*
- **The system shall** pass the operator identity as `approver` to every discard,
  using the same `ide:<user>` fallback `act` already applies. *(A discard's approver is
  an audit/rejection record — `discard` marks the pending approval row `rejected` — and
  discarding never needs an approval, so it is always supplied.)*
- **When** `--force` is absent, **the system shall** pass **no** approver to the merge
  and **shall** propagate the merge gate's refusal (`needs_approval`), leaving the race
  as it was. *(`merge`'s `approver` parameter **is** the approval: supplying one
  records it. Absent `--force`, the verb has no human approval to record and the gate
  must decide.)*
- **When** `--force` is present, **the system shall** record the operator identity as
  the merge's approver against the tree's sha. *(This is the reading `board merge` and
  the run page's Merge button already follow: `--force` does not bypass the gate, it
  answers it.)*
- **The system shall** resolve the winner and every discard through the same
  `workspaces.load` the page's grouping uses, and **shall** refuse the whole act when
  the winner has no open workspace.

#### FR-031: The intake ledger records which run an arrival started
`intake_events` has no `run_id` (verified schema: `id, provider, delivery_id,
payload_hash, issue_uid, thread_id, action, detail, created_at`). `intake/dispatch.py`
calls `store.record_intake_event(...)` with `thread_id`. A thread can start several
runs, so the thread is not a proxy for the run.

- **The system shall** add a nullable `run_id TEXT` column to `intake_events`.
- **The system shall** add the column on **both** schema paths: to `SCHEMA_SQL` in
  `mini_ork/threads/store.py` (fresh databases) **and** as migration
  `db/migrations/0075_intake_events_run_id.sql` (existing databases). *(Correction made
  during implementation: this spec first cited `0074`, which was already taken; the
  number is allocated at write time, not at spec time.)*
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
  the run is actually started. *(Implementation note: `intake/report.py`'s
  `record_intake_event` call deliberately still passes none. It records a **reported**
  arrival — the ledger row for a ticket the reporter described — which starts no run by
  construction, so the default is the correct answer there, not a missed call site.)
  `dispatch.py` is the only caller that starts runs, and it is the only one that
  changed.*

#### FR-032: Arrivals link to their runs; old rows degrade
- **The engine shall** render the arrivals ledger's run cell as an openable run when
  `run_id` is present.
- **When** `run_id` is NULL — every row written before this feature — **the engine
  shall** render an em dash, not an error and not an empty action.
- **When** `run_id` is NULL, **the engine shall** render no run action at all. *(Absent,
  not disabled: a disabled button implies the state is temporary. An arrival that
  started no run will never have one to open.)*
- **The engine shall** probe whether the `run_id` column exists before selecting it,
  and **shall** render the ledger without it when it does not. *(The IDE never migrates
  a home, so a page can be the first thing to open a `state.db` an older release wrote.
  `_rows` swallows `sqlite3.OperationalError` into `[]`, so assuming the column would
  not raise — it would silently blank every row the ledger has, which is the failure
  this requirement exists to prevent.)*
- **The engine shall** render a per-source sync action using the existing
  `intake once --source <S>` verb, so a source can be polled on demand.
- **When** a source is not registered, **the engine shall** render no sync action for
  it — there is no adapter to call, and a button that fails on stderr is worse than no
  button.

#### FR-033: Race decision panel
- **The engine shall** render one entry per race, not one table row per race, because
  the contestants are the decision input and belong on screen beside it.
- **The engine shall** show per-contestant: lane, run id, verification outcome, check
  counts and cost, and an action opening that run.
- **The engine shall** render each "keep" action as `S.cli("board","race-keep",<winner>,
  "--discard",<sibling>,...)` carrying a `confirm` that names the run being kept, the
  runs being discarded, and the worktrees freed — populated from a `--dry-run` result
  so the numbers are real, not estimated by the page. *(The emitted argv carries
  `--force` because the confirm **is** the human approval, the same doctrine the run
  page's Merge button follows; `--force` records the approval rather than bypassing the
  gate — see FR-030.)*
- **The engine shall** render a keep action as disabled, with the reason stated in the
  section note, when the plan cannot be built. *(A button that refuses on click is a
  worse answer than one that says why it is off.)*
- **The engine shall** draw only the newest `_RACE_PANEL_MAX` races in full and
  **shall** summarise the remainder behind a single "Earlier races" entry.
- **The engine shall** group a run by its kickoff's title. **When** a kickoff cannot be
  read, **the engine shall** draw that run as its own race rather than guessing it into
  someone else's. *(Fail-closed has a visible cost — the run appears alone — and that is
  the correct direction when the next step discards worktrees. `board race-keep` refuses
  on the same read, via the shared `acp.history.kickoff_key`.)*
- **The engine shall** take the per-contestant check counts from the same helper the
  run's outcome card uses (`run.verifier_check_counts`), so the two pages cannot report
  different numbers for the same run. *(This is P2's "no re-walk" point honoured: the
  race panel shares the definition rather than re-deriving it.)*

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

*Satisfied by FR-031 + FR-032 — unit-level. Unit evidence:
`test_an_arrival_that_started_a_run_links_to_it` (the row's fifth cell is the run id
and its act is `Open run`, `do.run == "run-abc"`);
`test_an_arrival_that_started_no_run_says_so_and_offers_nothing`. The live half — a
real ticket through `intake/dispatch.py` into the ledger — is unrun; see the
cross-cutting live-replay item.*

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

*FR-040 addition:* that same error map is one of the attention strip's four inputs, so
the finding now rides **every** tab as the strip's `{n} error(s)` item, with the
re-read button that answers it. The strip is built after the reaper's error is merged
and before the banner, which is what lets one read failure reach both. Unit evidence:
`test_an_unreadable_profile_is_a_strip_item_and_a_banner_line`.

### AC-005: J5 — Answer a blocked run
**Given** a run whose profile status is `needs_answers` with one outstanding question,
**When** the engineer submits an answer from the blocked-on-you panel,
**Then** `profile-answers.json` contains the answer, `run_profile.json`'s status reads `ready`, its `human_questions` is empty, and the command returns a `next_step_cli`.

### AC-006: J6 — Decide a race
**Given** four runs sharing one kickoff, all with open worktrees,
**When** the engineer confirms the keep action for one of them,
**Then** exactly that run's branch is merged, the other three worktrees are discarded, and the result reports per-run outcomes — **and** when one discard fails, the result is `ok: False` and names the failed step while the other discards still complete.

*Satisfied by FR-030 + FR-033 — unit-level, with the git seams replaced. Unit
evidence: `test_keep_merges_the_winner_then_discards_the_rest` (merge order, discard
order, `discarded`/`failed` payload);
`test_a_partial_discard_failure_is_reported_not_masked` (rc 1, `step: "discard"`,
`discarded == [OTHER]`, `failed == [{LOSER, …}]`, and the loop continued);
`test_one_keep_per_contestant_naming_the_others` + the seam
`test_the_emitted_keep_argv_is_one_the_cli_accepts` (the page's button argv parses and
plans through the real verb). A real `git merge` against a real worktree is unrun —
that is a side effect on the shared engine, so `workspaces.merge`/`discard` are
stubbed here; AC-006's live replay needs a runner.*

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

*Satisfied by FR-023 — unit-level, and rendered with existing vocabulary only (no fork
change). Unit evidence: `test_twelve_profiled_runs_all_appear_ordered_by_confidence`
(twelve rows, the confidence column ascending, the note reading `2 of 12 below
confidence 0.7`, on a fixture where the doubtful runs are neither the newest nor
adjacent); `test_selecting_a_row_shows_that_runs_plan_not_the_newest_runs` (the picked
run is the *newer, higher-confidence* one — the panel proves the click, not the
default); `test_argv_the_ide_sends_selects_the_row` (`--arg run_id=` through the real
`board page` argv). The live half — a real router mis-route reviewed and acted on — is
the same cross-cutting replay item; J9's correction path stays out of scope (§ Out of
Scope), so the table is read-only.*

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

*Verified 2026-10-16 against the fork, not by inspection alone.* Every tab of the
redesigned page was built on the live home (`/Users/admin/ps/mini-ork/.mini-ork`,
1628 `task_runs`) through the exact CLI route the IDE calls —
`board_cmd page orch --tab <t> --home …`, for all eight of `TABS` — and the
resulting payloads carried `errors: {}` and nine section types:

| emitted on the live home | `page.rs::render_section` arm |
|---|---|
| `triage` (×8) | :843 |
| `chips` (×5) | :876 |
| `pills` (×1) | :875 |
| `table` (×8) | :853 |
| `list` (×11) | :855 |
| `composer` (×6) | :847 |
| `kv` (×1) | :854 |
| `code` (×1) | :874 |
| `callout` (×1) | :844 |

The action side is the half a section table does not cover, because every button
the redesign adds is a dict the *store* has to know how to carry out. The kit's
action builders (`spec.py`) emit twelve keys, and `store.rs::perform` (:476)
handles each: `cli` :483, `page`(+`tab`/`args`) :487, `set` :513, `run` :516,
`path` :537, `reveal` :544, `url` :546, `thread` :548, `project` :550,
`project_pick` :552, `pick_files` :554, `project_forget` :556. `open_task`
resolves to `page: "task"` and so rides :487.

Two things this check settles that "no changed file under `crates/`" does not:

- **`set` is the picker idiom, and it is handled.** The classifications table's
  row activation (FR-023) and the Races recipe pills (FR-016) both work in the
  fork only because `store.rs:513` turns `{"set": {...}}` into
  `page.set_args(...)` → `reload()`. A fork without that arm would draw the
  table and drop every click.
- **`confirm` guards reach every affordance.** `S.cli(..., confirm=…)` — the
  `--force` revive (FR-021) and the race-keep discard (FR-014) — is honoured by
  `page.rs::action_handler` (:521), which is the single path every button, row,
  chip and lane card takes (`page.rs:558,970,1150,1302`,
  `sections.rs:460,645,944,1281,1391`, `flow.rs:927,1185,1609`). Nothing in the
  redesign can fire a destructive verb unguarded.

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

### P0 — Wiring (no new verbs, no schema) — **implemented 2026-10-15, tested 2026-10-16**
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
- [x] Tests: a unit test per section, asserting the new keys exist and that a missing
      source degrades rather than raising. **Written 2026-10-16** —
      `tests/unit/test_ide_pages_orch_sections.py` (25), **25 passed**. One section per
      group: FR-001's banner (absent when the map is empty, one line per error, first
      section of the page is the strip and the last is the banner), FR-002 (`_finding_text`'s
      `{glyph} {msg} — {fix}`, the row's lint text and recipe, `(default)` when the file
      declared none, the `Log` button only when `_run_log` finds a file, a lint *error*
      still rendering red rather than being hidden), FR-003 (the three pickers, each
      marking exactly one chip and writing `features set-env <VAR>` with `home=False`, the
      lane picker reporting `MO_ACP_DEFAULT_MODEL` first, and an unreadable features store
      degrading to a provenance instead of blanking the picker), FR-004 (a thread row
      opens the thread, a run row the run, costs summed, and the empty history still
      rendering a non-link placeholder), FR-005 (`_open_abs` refusing a relative path,
      `_concord_stop`'s `--yes` + confirm + `home=False`, `_coord` degrading without
      ContextNest) and FR-006 (states confined to `pass`/`na`/`fail`, a trigger row only
      for a registered *and enabled* source, an unconfigured trigger failing its row, the
      scheduler last and reporting its last tick, an unreadable scheduler failing rather
      than passing, and the checklist appearing at IDE spec level 2 but not level 1).
      **One defect found and fixed:** `_chain_health` computed `_SOURCE_STATE[...]` and
      then discarded it, rendering a registered-but-disabled source as `pass` — while its
      own docstring promises `na`, and the intake table beside it shows `disabled`. The
      two views of one source disagreed; the row now reads `na` with
      `registered but switched off`, the same reading `_SOURCE_STATE` already gave the table.
- [x] **Add the orch tabs to `tests/unit/test_ide_pages_actions.py::_PAGE_TABS`.**
      It listed only `intake`, so the `cli` actions FR-003 and FR-005 add were outside
      the argv walk — the exact gap the `kanban` comment in that file describes.
      Extended to all eight tabs (`session, dispatch, decide, kickoffs, races, planner,
      coord, intake`) as part of P2. `pytest tests/unit/test_ide_pages_actions.py` →
      **4 passed**, so every action on every tab parses against its own parser.
- [ ] **Stale assertions in `tests/unit/test_ide_pages_orch.py`: 7 failures — 5
      pre-dating P2 and caused by P1's own design changes, one caused by
      P3's FR-033, and one caused by FR-023 — all left unfixed per the working
      agreement (tests are not touched until the implementer's change is confirmed).**
      The seventh, `test_planner_reads_the_latest_profile_and_plan`, asserts the
      pre-FR-023 shape of the Planner tab: it indexes `sections[0]` and expects the
      classification `kv` there. FR-023 puts the classifications table first, because
      the table is what you pick from and the detail is what follows the pick;
      `tests/unit/test_ide_pages_orch_classify.py` is its replacement.
      The sixth, `test_races_group_contestants`, asserts the pre-FR-033 shape: one
      table row per race. FR-033 deliberately reversed that (one *section* per race, the
      contestants as its rows) because the contestants are the decision input and the
      Keep buttons have to sit beside them — the test's own name is now the old
      requirement. `tests/unit/test_ide_pages_orch_races.py` is its replacement.
      None is a P2 regression; each asserts a shape P1 deliberately changed:
      `test_every_tab_builds` pins the tab key list (`dispatch` added by FR-012);
      `test_thread_defaults_follow_the_env`, `test_mcp_pills_are_the_servers_tools` and
      `test_history_has_threads_and_runs` index `sections[0]`/`[1]` positionally, and
      FR-003 turned the session tab's leading `kv` into three `chips` pickers;
      `test_kickoffs_are_linted_and_link_their_runs` expects the pre-FR-002 sub text
      (`"lint: The kickoff…"` where `_kickoff_parts` now writes
      `"saved · lint: ⚠ The kickoff…"`). The remaining 4 tests in the file pass.
      A **seventh stale failure lives in another file**:
      `tests/unit/test_native_dispatch_py.py::test_all_former_exec_subs_registered_natively`
      pins `_NATIVE_MODULE_SUBS` by exact set, and FR-011 added `kickoff` to it. Left
      unfixed under the same agreement — and worth naming here rather than leaving a red
      suite unexplained, because it is the one stale assertion the P1/P2 notes missed.

### P1 — Dispatch
- [x] FR-010 — `board start`: add `start` to `build_parser().choices`; add `--kickoff`, `--set` (append), `--run-id`, `--workspace`, `--init`, `--dry-run`; exempt `start` from `validate`'s run-id rule and require `--kickoff`; implement `_act_start` wrapping `web.control.launch_run`; fail closed on an uninitialised home.
      Verified by hand (10 probes, `.venv/bin/python`): no `--kickoff` → rc 2; **uninitialised home → rc 2 and no `runs-inbox` created** (fail-closed); `--init` proceeds; recipe resolves from front matter → `--recipe` → refusal; `--set X=1`/`--workspace` map into `extra_env`; a stubbed `launch_run` receives `(home, recipe, text, run_id, extra_env)` and a kickoff body; `launch_run` returning `ok:False` → rc 1 with the error on stderr. A **live spawn was not run** — starting a real run is a side effect on the shared engine, so `launch_run` was stubbed instead; AC-001's live replay needs a runner.
- [x] FR-011 — new `mini-ork kickoff new <text>`: `mini_ork/cli/kickoff_cmd.py`, registered in `main.py`'s `_NATIVE_MODULE_SUBS` as `"kickoff"`. Template writer via `kickoff_lint.slug`; seeds front matter with `--recipe` → `MO_ACP_RECIPE` → `code-fix`; `## Success` stub; refuses to overwrite and prints the existing path; prints the written path on stdout.
      Verified by hand: drafted `fix-the-auth-middleware-timeout.md` lints **0 findings**; `MO_ACP_RECIPE=refactor` seeds `recipe: refactor`; `board start --dry-run` on a draft reads back `recipe=code-fix source=front matter` — the write→read loop closes. Overwrite → rc 2; empty goal → rc 2; no subverb → usage, rc 2.
- [x] FR-012 — `orch`: the `dispatch` tab and its row actions. TABS gained `("dispatch", "Dispatch")` (second). Sections in FR-012's order: page-scoped recipe/workspace chips (`S.set_args`), the kickoff list, the `composer` bound to `S.cli("...", ["kickoff","new"])`, the lint panel for `args["kickoff"]`. `_kickoffs_section` was refactored onto a shared `_scan_kickoffs`/`_lint_badge`/`_kickoff_parts` so the two tabs cannot disagree.
      Verified by hand: all 7 tabs build at **both** IDE spec levels with no `errors`; the lint panel appears only when a kickoff is selected (checks at level 2, markdown fallback at level 1); a walked **23 `cli` actions parse against their own parsers, 0 failures** (`features` rejects `--home` and every such action carries `home=False`); AC-003's chain reproduced end to end — composer writes the file, the dispatch list shows it, its Start action is present, selecting it renders the lint panel. A live spawn: see FR-010.
- [x] Tests: parser round-trip (mirroring `tests/unit/test_board_cmd.py`'s guard), dry-run writes nothing, uninitialised home refuses, and a real spawn is asserted from `launch_run`'s return rather than by waiting for the run. **Written 2026-10-16** — `tests/unit/test_board_start.py` (19) and `tests/unit/test_kickoff_new.py` (11), **30 passed**. Every test drives the real `board start` / `kickoff new` argv through its own `main`, so the parser, `validate` and the exit-code split are on the path; `launch_run` is stubbed (a real spawn is a side effect on the shared engine), and the *one* thing asserted against real disk is the refusal — an uninitialised home must leave no `runs-inbox`, which is what `launch_run` would have created. `kickoff new` is asserted through the two readers that consume its output (`orch._scan_kickoffs` and `board start --dry-run`), not against the template's own text, so the write→read loop is the assertion.
      **Two defects found and fixed in `board_cmd.py`:**
      1. **`--run-id` was silently ignored.** `--run-id`'s dest collided with the positional `run_id`, and argparse writes the positional's default over an option that shares its name — `board start --run-id run-plan --kickoff K` parsed to `run_id is None` and launched under a generated id. The option now has its own dest (`start_run_id`). Nothing else read it, and the page never passes it (it lets the id be generated), which is why no walk found it.
      2. **The dry run under-reported the workspace.** `_start_env` returned only the flag-derived pairs, so `board start --dry-run` with `MO_WORKSPACE_MODE` inherited from the environment reported `workspace: ""` beside `workspace_source: "MO_WORKSPACE_MODE"` — a source naming a value it did not show. `_start_env` now returns the effective mode as well, which is what FR-010 requires the dry run to report.
      Also recorded by these tests, deliberately: `board start` consumes neither positional slot, so `board start run-x --kickoff K` launches under a *generated* id (pinned, because `--run-id` and the positional read alike), and a single stray positional is tolerated while two are a usage error.

### P2 — Answers — **implemented 2026-10-16**
- [x] FR-020 — `orch._recent_profiles(home, errors, limit=60)`: the newest 60
      `task_runs` rows, each run's `run_profile.json` read directly, sorted by the
      profile file's mtime (longest wait first). A run dir with no profile is skipped
      silently; an unparseable one is recorded under `errors["profile:<run_id>"]`, which
      FR-001's callout then surfaces.
      *Deviation, deliberate:* the `needs_answers` predicate is restated
      (`profile_status == "needs_answers" and pending`) rather than delegated to
      `web.control.get_profile`, because `get_profile` catches `JSONDecodeError` and
      returns `{}` — it cannot distinguish "never asked" from "will not parse", which is
      the distinction FR-020 requires. The predicate is the same two terms.
- [x] FR-021 — `board answer <run_id> --text T [--for Q ...]`. `--text` became
      `action="append"` (it was a single `store` shared with `steer`); the `steer`
      branch joins the values (`" ".join`), so a one-`--text` steer is unchanged and the
      repeatable-pair form is expressible. Refusals: several questions with a bare
      `--text` (lists the ids); no pending questions; unknown `--for`; `--for`/`--text`
      count mismatch; a question named twice; an unknown run (`stage: "usage"`). Every
      refusal writes nothing. Success returns `save_answers`' payload verbatim, so
      `next_step_cli` rides through unchanged. `answer` was added to the stderr-writing
      verb set so the reason is visible on the CLI.
      `--for` accepts either the `q<N>` id or the question's own text.
- [x] FR-022 — the `decide` tab (`TABS`, third): one `list` per parked run, titled
      `Blocked on you · <short>`, longest wait first, capped at `_DECIDE_LIMIT` (10)
      with a pointer to the Planner tab past it. Wait duration on every row; tone
      escalates with age (`sub` → `yellow` ≥15 m → `orange` ≥1 h → `red` ≥3 h) and past
      `_WAIT_ALERT_S` (1 h, `MO_DECIDE_ALERT_S` to tune) the row also names the recipe
      and the start time. One `composer` per *question*, bound to
      `["board","answer",<run>,"--for",q<N>,"--text"]` — per question, not per run,
      because the verb refuses a bare `--text` on a multi-question run.
- [x] Tests — `tests/unit/test_board_answer.py` (11) + `tests/unit/test_ide_pages_orch_decide.py` (12), **22 passed**. Covered: single-question success flips the status and clears the questions; several-pending refusal lists the ids *and* writes nothing; paired `--for/--text` answers several in one call; `--for` by text; unknown id; count mismatch; no-pending; never-asked; unknown run; `--text` required; `steer` still reads its text; only parked runs listed, longest first; an answered and an unprofiled run excluded; per-question composers; id preserved for the unanswered half of a partly answered run; tone escalation and colour validity; the long-wait detail; an unreadable and a non-object profile into `errors`; a dict question normalised to its text. Plus the seam: the emitted composer argv, with the typed text appended, runs through `board_cmd.main` and answers the right question.
- [x] Cross-check — `pytest tests/unit/test_ide_pages_node.py test_ide_pages_inbox_kanban.py test_ide_pages_thread.py test_ide_header_lanes_composer.py` → **95 passed**, i.e. the shared `--text` change did not disturb `steer`.
- [x] FR-023 — **the classifications table (AC-009, J9), the phase's own second journey.**
      `orch._planner(home, args, errors)` now emits `S.table("Classifications")` —
      every run the FR-020 scan found, one row per run (`run`, `task_class`, `recipe`,
      `conf`, `outcome`) — followed by the *selected* run's detail
      (`_classification_detail`). `_recent_profiles` gained `run_dir`, `task_class`,
      `confidence`, `status` (its `SELECT` widened to match), so the table and the
      Decide tab read **one** walk: the seam P2 and P3 each noted as owed, now taken.
      Ordering is by confidence, lowest first (`_confidence_rank`), with a
      profile that never recorded one sorting **last** — the profile gate is what
      writes `confidence`, so a gate-skipping recipe (epic-runner) must not be
      ranked as the worst decision the router ever made. The note counts the rows
      under `_LOW_CONFIDENCE` (0.7): `{low} of {n} below confidence 0.7`.
      Selection is `S.set_args(run_id=…)` on the row **and** on its `Review` button —
      the same action twice, so a row is clickable anywhere and the affordance is also
      a named control. The detail follows the selection, defaulting to the first row
      (the least confident run), so the panel can never detail a run the table is not
      highlighting. An `--arg run_id=` naming no row is ignored: the id is a string
      from the IDE, and joining it into a path unvalidated would make the picker a
      file read. `_latest_run_with` — the newest-profiled-run helper this replaces —
      is deleted; it had no other caller.
      *Renderer:* no fork change. `table` + `set` + `sel` are all existing vocabulary
      in `crates/mini_ork_ui` (`page.rs` row `do`, `store.rs` `set`, `page.rs` `sel`),
      so AC-012 holds.
- [x] Tests — `tests/unit/test_ide_pages_orch_classify.py` (13), **13 passed**. Covered:
      AC-009's own fixture — twelve profiled runs, two below 0.7, the confidence column
      in ascending order and the note naming `2 of 12`; a gate-skipping recipe's missing
      confidence read as `—` and sorted last; the row's five cells; `task_class` falling
      back to the `task_runs` column; the three outcome tones; click and `Review` sharing
      one action; selecting a row showing **that** run's plan and objective — the newer,
      higher-confidence run, i.e. not the default, which is what proves the click drives
      the panel; the single `sel` row, and by default the first; three unusable
      `run_id` args (a traversal string, a stranger id, empty) each falling back; a run
      with no plan artefact still detailing its classification; the end-to-end
      `board page orch --tab planner --arg run_id=…` argv, with `page["args"]` echoed;
      an unreadable profile named in `errors` **and** in the banner; an empty home
      saying so.
- [x] Cross-check — `test_ide_pages_orch_classify.py + test_ide_pages_orch_attention.py +
      test_ide_pages_orch_decide.py + test_ide_pages_orch_races.py +
      test_ide_pages_orch_arrivals.py + test_ide_pages_orch_sections.py` → **89 passed**;
      `test_board_cmd.py + test_ide_pages_actions.py` → 153 passed with the two
      pre-existing failures named below. Blocking lint (`ruff check mini_ork/ tests/`)
      and the advisory tier on both changed files: clean.
- [x] **Closed — nothing functional remains.** The P3 note's last sentence is now closed
      too: the classification table *is* the reader that wanted `_recent_profiles`, and
      it reuses the walk rather than re-walking it.

### P3 — Races and arrivals — **implemented 2026-10-16**
- [x] FR-031 — `run_id` column: `SCHEMA_SQL` in `mini_ork/threads/store.py` (`INTAKE_COLUMNS`) + `db/migrations/0075_intake_events_run_id.sql` with the same `.read "|sh -c …"` `PRAGMA table_info` guard 0072/0074 use; `record_intake_event` gained `run_id: str | None = None`; `intake/dispatch.py` passes it where the run is started. *Migration number corrected from this spec's `0074` to `0075` — 0074 was taken.*
- [x] FR-030 — `board race-keep <winner> [--discard RUN_ID ...] [--dry-run] [--force]` in `mini_ork/cli/board_cmd.py` (`_act_race_keep`, `verb` choice, `--discard` append, `validate` requires the run id, usage + stderr sets updated). Resolves both ends through `workspaces.load`, verifies kickoffs match via the shared `acp.history.kickoff_key`, dry-run plan, per-run outcome reporting, `needs_approval` passthrough, and nothing discarded unless the merge landed.
- [x] FR-032 — `orch._arrivals` probes for the column and renders the run as an openable row action when present, an em dash with no action when NULL; `orch._intake`'s Sources rows carry a per-source `intake once --source <S>` act when the source is registered.
- [x] FR-033 — `orch._races` + `_race_keep_action`: one `S.table(f"Race · {title}")` per race with the contestants as rows (lane/run/outcome/checks/cost/worktree, each row opening its run), one Keep per contestant with a `--dry-run`-populated `confirm`, `_RACE_PANEL_MAX = 5` newest in full plus an "Earlier races" summary.
- [x] Tests — `tests/unit/test_board_race_keep.py` (15) + `tests/unit/test_ide_pages_orch_races.py` (14) + `tests/unit/test_ide_pages_orch_arrivals.py` (8) + 4 appended to `tests/unit/test_threads_store.py` (now 21), **58 passed**. Covered: dry-run touches nothing; an adopted worktree is not reported as freed; a repeated `--discard` is one worktree; cross-kickoff and unreadable-kickoff refusals call nothing; a winner with no workspace refuses; discarding the winner refuses; a missing run id exits 2; the merge message names the run; without `--force` the merge asserts no approval while the discard still records `ide:`; a refused merge stops before the first discard; a partial discard failure reports `discarded`/`failed` and continues; the reason reaches stderr. Pages: one section per race with exact argv and confirm text; the seam test feeds the emitted Keep argv back through `board_cmd.main([*argv[1:], "--dry-run", …])`; a keep that cannot be planned is off with the reason in the note; an unreadable kickoff makes a run its own race; the arrivals seam runs the emitted Sync argv through `intake_cmd.main` and asserts nothing started; the ledger renders without the column.
- [x] Cross-check — `test_ide_pages_orch.py + test_ide_pages_actions.py + test_ide_pages_orch_decide.py + test_code_findings.py` → **supplemented**, see the stale-assertion note under P0.
- [x] **Closed — nothing functional remains.** FR-033 shares `run.verifier_check_counts` rather than re-walking, and the classification table — the reader P2's note and this one both named as the only thing that would want `_recent_profiles` — now reuses that walk (FR-023).

### P4 — Attention — **implemented 2026-10-16**
- [x] FR-040 — `orch.attention(home, tab, errors) -> dict | None`: one `S.triage` strip
      from four inputs, each read through the helper its own tab reads it with —
      `_waiting_runs` (FR-020's `_recent_profiles`, filtered on `needs_answers`),
      `_undecided(_race_groups(home), _open_workspaces(home))` (FR-033's grouping, now
      extracted out of `_races` so the panel and the strip share **one** definition of
      "the same race"), `_failing_links` (FR-006's `_chain_health` rows whose
      `state == "fail"`) and the page's own `errors` map (FR-001). `None` when all four
      are empty — no empty strip.
      Each item is a pill (`{n} waiting` / `{n} race(s)` / `{n} chain link(s)` /
      `{n} error(s)`) beside the button that resolves it: `page_link("orch",
      tab="decide"|"races"|"intake")`, and for the errors a re-read of the tab being
      rendered — `page_link("orch", tab=<current>)`, which `page.rs::open_page` *does*
      reload, because a `page` action carrying no tab only focuses the tab it is already
      on. The intakes' pill text is the failing links' own row names, so the strip says
      *which* link is broken without a click.
      The strip is composed in `board_cmd.build_payload`, not `orch.build`, because the
      reaper's error joins `payload["errors"]` after `build_page` returns, and an error
      the strip cannot see is one it cannot report. It is built **before**
      `errors_callout`, so a profile the scan cannot parse reaches the banner as well as
      the strip — `attention` writes into the page's own map (`S.page` copies it, so the
      caller passes the copy the payload holds).
- [x] `_source_statuses(home)` extracted: the one registry read the Intake tab and the
      strip both need, so a source cannot be declared on one and absent from the other.
- [x] Tests — `tests/unit/test_ide_pages_orch_attention.py` (17), **17 passed**. Covered:
      no strip on any of the eight tabs when the chain is intact and nothing is parked; a
      parked run's pill, detail, tone, icon and `Answer` action, with the strip drawn
      first; one strip on every tab; two parked runs read as two; an answered question is
      no longer waiting; a race holding two worktrees is undecided and a kept one is not;
      a lone run is not a race; an unconfigured trigger and a missing scheduler each name
      their own link; an unreadable profile is a strip item *and* a banner line; the
      errors item re-reads the tab it is on; the waiting count equals the Decide tab's own
      entry count; the race count equals the races the panel draws; the chain count equals
      the checklist's own `summary.failing`; one button per item and none for an empty
      input. Every test drives the real `board page orch --tab <t>` argv, so the wiring in
      `build_payload` is exercised, not just `attention`.
- [x] The chain is *stated* in the tests rather than inherited: the source list comes from
      the machine's `pm.yaml` and the scheduler tick is a LaunchAgent under the user's
      home, so neither is a property of a temporary home. Both reads go through the two
      seams above. The consequence is worth stating plainly — **on a home with no
      scheduler tick installed the strip is never empty**, because the chain that starts
      runs really is incomplete; that is the requirement working, not a defect. (On this
      machine's primary home the tick *is* installed, and the strip reports the real
      backlog: 4 runs parked on `needs_answers`.)
- [~] **Partly done:** the live replay — the CLI half ran on the primary home
      (see Cross-cutting); the GUI half is the user's to run.

### Cross-cutting
- [x] NFR — warm-page budget held on the default tab, **measured, not assumed**.
      In-process, warm, against a real home with **1303 run directories** (the heaviest
      input the scan can be given), `board_cmd.build_payload` with the strip switched off
      vs on: `session` 677 → 721 ms, `dispatch` 1017 → 1013 ms, `decide` 18 → 36 ms,
      `races` 19 → 36 ms, `intake` 25 → 48 ms. The strip costs **≈20–45 ms per tab**
      (min of three runs; a single cold reading of the same home came out at 5.9 s and is
      disk-cache noise, not the strip). Every tab stays far inside the 1.5 s warm target,
      and the strip adds no network call — `source_statuses` and `scheduler_status` are
      file/DB reads, as is `_recent_profiles`' 60-directory walk.
- [x] NFR — FR-023 did not move that budget. It reuses the same 60-directory walk
      (no new scan reads the disk) and returns 58 rows on the primary home; three warm
      reads of `board page orch --tab planner` end-to-end, interpreter start included:
      **0.22 s / 0.16 s / 0.14 s**. The tab draws every scanned run rather than a
      capped page of them, and at 58 rows that is still an order of magnitude inside
      the 1.5 s target.
- [x] NFR — AC-012 holds for **the redesign**: no section this spec adds needed a
      renderer change. `crates/mini_ork_ui/src/sections.rs` *was* edited, but by the
      audit's rows 4-6 — pre-existing engine↔fork drift, authorised separately. See
      the conformance section.
- [~] Live replay for the ACs of each shipped phase, per the repo's gate-every-run-by-hand
      habit. Split in two, because only one half is the engine's to run:

      **CLI half — done 2026-10-16, on the primary home** (`/Users/admin/ps/mini-ork/.mini-ork`,
      1628 `task_runs`, 58 of them profiled). Every one of the eight `TABS` was built
      through the argv the IDE sends (`board_cmd page orch --tab <t> --home …`) and the
      payloads came back with `errors: {}` — no tab depends on a fixture to render. The
      AC most at risk from real data is AC-009, and it holds: the classifications table
      returns **58 rows, confidences ascending** (`0.70, 0.75, 0.75 … 0.95`), exactly one
      `sel` row, and row 0 selected by default so the table can never point at a run the
      detail below is not showing. Sending a *middle* row's id back as the IDE would
      (`--arg run_id=run-1792068761-cnui-ctx-b`) moved the mark to that row and produced
      `Classification · 1-cnui-ctx-b` and `Plan · 1-cnui-ctx-b` carrying that run's own
      objective — the J9 correction path, end to end, on real runs.

      The live home also fixes what the head of the list means: its note reads
      **`0 of 58 below confidence 0.7`**. The ordering *rule* is exercised by the
      twelve-run fixture (AC-009); on this home there is no doubtful row to put at the
      top, which is the requirement's own success state, not an untested path.

      The same run doubles as the AC-012 payload census: **155 `{"set": …}` actions, 43
      `cli`+`confirm`+`home` and 1 `cli`+`confirm`** (the guarded destructive verbs), plus
      `page`(+`tab`/`args`), `run`+`title`, `path` and `thread` — every live `do` key is one
      `store.rs::perform` handles, so no affordance on the redesigned page is a control
      that draws and does nothing.

      **GUI half — owed, and it is the user's to run.** Launching the IDE and clicking
      the tabs is a front-end launch, which the standing instruction keeps off this
      session's plate; it also needs the real window to confirm the *pixels* (the
      conformance audit's row-1 fix — a list-act's `kind` — is only observable drawn).
      What to look at, in order: the Orchestrator rail entry, then each tab for its
      section set (`Session` = triage + 3 chips + pills + table, `Coordination` =
      triage + 4 tables + 2 lists + code), a `confirm` prompt on any `--force`-guarded
      action, and a classifications row click re-reading the detail beneath it.

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
| 4 | `columns` section `title`/`note`/`actions`, card `id` | `render_columns` reads only `cols` (`sections.rs:339`) | `kanban.py:601` passes a `note` nobody sees | draw the generic card header for columns — **fixed** |
| 5 | `story_step` `kind`/`model`/`meta`/`open`, section `title` | `render_story` reads neither | `run_story.py:432` passes `"What happened"` — invisible | draw the section title; surface `meta` — **fixed** (the `open` half was already honoured by `page.rs`'s `seed_step_expansion`, which the audit missed) |
| 6 | `finding["source"]` | `render_findings_body_inner` reads 6 of 7 | minor | draw or drop the key engine-side — **fixed** (drawn; the key has a reader on the story tab) |
| 7 | **`tone="warn"`** (`cn_file.py:554`) | not a colour in `palette.rs:200-214` | a callout silently loses its colour and mark — **an emitter bug, not a renderer gap** | change the emitter to `orange` — **already resolved**: `cn_file.py` no longer emits `tone="warn"` |

**AC-012 tension, resolved.** AC-012 ("no changed file under `crates/mini_ork_ui/`")
was scoped to *the redesign*: its sections must need no new renderer work, and they
don't — the locked architecture decision holds. The gaps above are **pre-existing
engine↔fork drift**, not redesign work, and the instruction that authorised this pass
was explicit that the fork must render what the engine emits. **All seven rows are
now closed** — rows 1-3 by the fork's first pass, rows 4-6 by its second
(`mini_ork_ui` `9283d3f`, amended to `0aefe69`), and row 7 in the engine, where the
emitter had already stopped sending `tone="warn"`. The fork pass is verified by
`script/mini-ork-check` (clean) and clippy parity against the pristine submodule
(19 flagged locations before and after; none inside the changed regions) — the
renderer has no unit tests by convention, so a pixel-level replay is still owed.
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
| `TABS`, `build`, `_kickoffs_section`, `_history`, `_thread_defaults`, `_races`, `_planner`, `_classify_table`, `_classification_detail`, `_coord`, `_intake`, `_arrivals` | `mini_ork/ide_pages/orch.py` | The page under redesign — **moved since the companion briefs** |
| CLI verb registry (no `kickoff` entry) | `mini_ork/cli/main.py` | `kickoff new` is a free top-level name; `run`'s `--set`/`--workspace` flags are the vocabulary `board start` mirrors |
