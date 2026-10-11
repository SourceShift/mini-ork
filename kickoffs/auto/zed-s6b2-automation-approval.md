# Zed S6b-2 — schedule a recipe from a Zed thread: ask → proposal card → buttons

Depends on S6b-1 merged (`automations.propose / commit_proposal / discard_proposal /
next_fires`, MCP `propose_automation` / `list_automations`, `/automations`,
`/automation …`). Serial with the other slices that edit `mini_ork/acp/agent.py`.

## Goal

A user types "run changelog-entry every weekday at 9 and record what merged yesterday" (or
`/automation new …`). The orchestrator picks the recipe, turns "every weekday at 9" into a
schedule and says it back in words, writes the kickoff each run receives, and proposes the
automation. The thread shows the proposal as a card — what runs, when, the next three
times, the kickoff — and the user decides with buttons: **Create automation** / **Change
something** / **Discard**. On Create, if the OS scheduler is off, one more question turns it
on, because an automation that never fires is the worst outcome. Same shape as recipe
drafts (S3b-2): the model proposes, the user creates.

## Files in scope

- `mini_ork/acp/agent.py` — proposal card, approval buttons, scheduler offer.
- `mini_ork/acp/commands.py` — `/automation new`; the rewrite sentinel's bridge line.
- `mini_ork/acp_orchestrator/prompt.md` — a "Scheduling a recipe" section.
- `tests/unit/test_acp_agent_py.py`, `tests/unit/test_acp_commands.py` — extend.

No other file changes.

## Mechanism (exact spec)

### Orchestrator prompt (append a section "Scheduling a recipe")

When the user wants something to run on a schedule ("every weekday at 9", "nightly",
"every Monday morning"): call `list_recipes` (and `describe_recipe` when unsure) to pick
the recipe; if none fits, offer to create one first (the recipe steps above). Ask only
what is missing, at most 2 short questions per message: which recipe, when, what each run
should do. Turn the time into a 5-field cron string in local time and say it back in words
("every weekday at 09:00"). Write the kickoff as a short task description each run
receives (a `## Files in scope` section when the recipe edits files). The default is a new
worktree per run; use in place only when the user asks. Then call `propose_automation`.
Never say an automation is scheduled or created — the user creates it with the buttons
under the proposal. `list_automations` shows the existing ones; to change one, propose
again with the same id. Keep ids short, lowercase, with hyphens.

### Proposal card (orchestrate turn)

- In `_prompt_thread_orchestrate`, track `propose_automation` tool_use ids exactly as
  `draft_recipe` ids are tracked (the name after the MCP prefix). On the matching
  tool_result whose JSON has `ok: true`: remember it as this turn's pending proposal (`id`,
  the tool call id, `exists`) and emit a `ToolCallProgress` for that tool call id with one
  `ContentToolCallContent` text block:
  `**<name>** runs \`<recipe>\` <when> (\`<cron>\`), <in a new worktree | in place>.` /
  `Next: <the three next_fires, as the /automations table formats times>.` / the kickoff in
  a fenced ```` ```markdown ```` block (first 40 lines, `…` when longer). Use the S6b-1
  `automation_view` time formatter. `ok: false` → nothing extra.
- When a turn produced both a recipe draft and an automation proposal, offer the recipe
  first (an automation of a recipe that does not exist yet would fail to create).

### Buttons (after the turn)

- After the orchestrator turn returns, if this turn produced a pending proposal:
  `request_permission(session_id, tool_call=ToolCallUpdate(tool_call_id=<tool id>,
  title=("Update" if exists else "Create") + f" automation {id}", kind="edit",
  status="pending"), options=[create ("Update automation" if exists else "Create
  automation", allow_once), change ("Change something", reject_once), discard ("Discard",
  reject_always)])`.
  - `create` → `automations.commit_proposal(home, id)`; ok → message `"Scheduled <name>:
    <when>. Next run <time>."` (or `"Updated <name>: …"`). Error → the error; proposal kept.
    Then, when `automations.scheduler_status(home)["installed"]` is false: emit
    `"Automations fire from a small background job that checks every minute — a
    LaunchAgent on macOS, a crontab line on Linux — even when Zed is closed. It is off in
    this project."` and `request_permission` on tool call id `automation-scheduler:<home
    hash or id>` titled `Turn on the scheduler` with `on` ("Turn on the scheduler",
    allow_once) / `later` ("Not now", reject_once). `on` → `install_scheduler(home)` →
    `"Scheduler on — /automation scheduler off removes it."` (error → its text); `later` /
    dismissed → `"Saved, but it will not fire until the scheduler is on: /automation
    scheduler on."`. Installed already → no question.
  - `change` → `"Tell me what to change."` (proposal kept).
  - `discard` → `discard_proposal`; `"Proposal discarded."`.
  - dismissed (`DeniedOutcome`) → `"Proposal kept — ask me to create it when you're
    ready."`.
  - Every message is recorded in the thread, as all thread output is. Mark the tool call
    `completed` on create, `failed` otherwise (as the recipe flow does).

### `/automation new [what]`

- Like `/recipe new`: the handler returns the rewrite sentinel with the orchestrator text
  `"The user wants to run a recipe on a schedule. <what, or 'Ask what should run and
  when.'> Follow your scheduling steps."`; mode-independent; in a run session →
  `"Automations are set up in a mini-ork thread — start one from New Thread."`. Announce it
  (`automation new`, "Schedule a recipe: describe what should run and when.").
- The sentinel `_RewriteToOrchestrate` gains `bridge: str | None = None`; `_dispatch_slash`
  emits `bridge` when set, else today's recipe text. `/automation new` sets: `"Handing
  this to the orchestrator. When it has a proposal you'll see it here with the buttons to
  create it."`.

## Tests

`tests/unit/test_acp_agent_py.py` (no pytest-asyncio — `asyncio.run`; keep the autouse
guard; a fake orchestrator turn emitting a `propose_automation` tool_use + tool_result built
from a real `automations.propose` in a temp home with a project recipe; a fake connection
with scripted `request_permission` outcomes that records calls; `scheduler_status` /
`install_scheduler` monkeypatched — never the real launchd/crontab): the card shows name,
recipe, when, three next times and the kickoff; `create` writes `automations.json`, says
"Scheduled", and offers the scheduler when it is off; `on` calls `install_scheduler`;
`later` gives the "will not fire" message; scheduler already on → no second question;
`change` keeps the proposal; `discard` deletes it; dismissed keeps it; `ok: false` asks
nothing; an existing id says "Update automation"; a recipe draft + proposal in one turn
offers the recipe first; `/automation new nightly changelog` reaches the orchestrator with
the rewritten text and the proposal bridge line.

`tests/unit/test_acp_commands.py`: `/automation new` in a thread returns the sentinel with
the bridge; in a run session the message; it is announced.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_agent_py.py tests/unit/test_acp_commands.py tests/unit/test_automations.py tests/unit/test_acp_automation_view.py tests/unit/test_mcp_context_server.py` passes.
- `ruff check` clean; diff touches only the files in scope.
