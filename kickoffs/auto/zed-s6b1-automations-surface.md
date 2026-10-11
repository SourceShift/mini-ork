# Zed S6b-1 — automations in Zed: proposals, MCP tools, /automations table and card

Depends on S6a merged (`mini_ork/automations.py`, `mini-ork automations …`). The approval
buttons and the orchestrator flow are S6b-2; this slice builds what they stand on.

## Goal

A user should see and control their scheduled runs without leaving Zed: `/automations` is a
table of every automation with its schedule in words, its next firing, its last run's
state and whether the OS scheduler is on; `/automation <id>` is a card; `run`, `pause`,
`resume`, `delete` and `scheduler on|off` act on them. For S6b-2, the orchestrator also
needs a way to *propose* an automation that the user then approves — never to schedule
one directly — exactly as recipes are drafted and then created with a button.

## Files in scope

- `mini_ork/automations.py` — proposals + `next_fires`.
- `mini_ork/mcp_context/server.py` — control tool `propose_automation`; read tool `list_automations`.
- `mini_ork/acp/automation_view.py` — NEW: table + card rendering.
- `mini_ork/acp/commands.py` — `/automations`, `/automation …`; help text; `/merge` message.
- `tests/unit/test_automations.py`, `tests/unit/test_mcp_context_server.py`,
  `tests/unit/test_acp_commands.py` — extend; `tests/unit/test_acp_automation_view.py` — NEW.

No other file changes.

## Mechanism (exact spec)

### `automations.py` additions

```python
def next_fires(expr: str, n: int = 3, after: datetime | None = None) -> list[datetime]
def propose(home, *, id, name, recipe, kickoff, schedule, workspace="worktree") -> dict
def get_proposal(home, id) -> dict | None
def commit_proposal(home, id) -> dict
def discard_proposal(home, id) -> dict
```

- `next_fires`: the next `n` firing times after `after` (default now) via `next_fire`;
  `[]` when the expression does not parse or never fires.
- `propose`: validate every field exactly as `add` does (id pattern, non-empty name and
  kickoff, recipe exists, cron parses, workspace value) but allow an `id` that already
  exists (that proposal edits it). Errors → `{"ok": False, "error"}`, nothing written.
  Otherwise write `<home>/automation-drafts/<id>.json` (`{fields…, "created_at"}`, atomic
  temp + rename; replaces an older proposal of the same id) and return `{"ok": True,
  "proposal": {fields}, "exists": <id already an automation>, "when": describe(schedule),
  "next_fires": [ISO strings, 3]}`.
- `commit_proposal`: no proposal → `{"ok": False, "error": "no proposal <id>"}`. Exists →
  `update(home, id, name=…, recipe=…, kickoff=…, schedule=…, workspace=…)` (keeps
  `enabled`, history); else `add(...)`. On `ok`, delete the proposal file. Return the
  `add`/`update` result plus `"created": <bool>`.
- `discard_proposal`: delete the file; `{"ok": True}` also when absent.
- `get_proposal`: the parsed file or None.

### MCP

- Control tool `propose_automation` (args: `id`, `name`, `recipe`, `schedule` — a 5-field
  cron string, `kickoff_markdown`, optional `workspace`) → `propose(...)`. Description:
  "Propose a scheduled run of a recipe. This does NOT schedule anything: the user sees
  the proposal and creates it with a button. Schedule is a 5-field cron string in local
  time (minute hour day-of-month month day-of-week; 0 = Sunday), e.g. `0 9 * * 1-5` =
  every weekday at 09:00. Calling it again with the same id replaces the proposal."
- Read tool `list_automations` (no args; available without `--control` too) → `{"automations":
  [{id, name, recipe, schedule, when, enabled, workspace, next_fire (ISO or null when
  paused), last_run: last_run_status(...), runs}], "scheduler": scheduler_status(home)}`.
- No tool that adds, fires, pauses or deletes an automation directly.

### `mini_ork/acp/automation_view.py`

```python
def render_automations(home: Path, *, now: datetime | None = None) -> str
def render_automation_card(home: Path, automation_id: str, *, now: datetime | None = None) -> str | None
```

- Table (same markdown table style as `recipe_view.render_recipes`):
  `| | automation | recipe | when | next | last run |`. Mark column: paused → `⏸`; never
  fired → `·`; else the last run's mark (`task_state.run_mark(status, run_dir)` with the
  status from `mini_ork.acp.history.list_runs`, one call for the whole table; a last
  firing that failed to launch → `✗`). `automation` = `name` + `` (`id`) ``; `when` =
  `describe(schedule)`; `next` = a short local time (`Mon 12 Oct 09:00`; `today 14:30`;
  `tomorrow 09:00`), `paused` when disabled; `last run` = `<run id> · <age> ago` (age as
  `/runs` shows it), `not started: <error>` when `last_error`, `never`.
- Footer line: scheduler on → `Scheduler on — last tick <age> ago.` (or `— no tick yet.`);
  off and any enabled automation exists → `Scheduler off — scheduled automations will not
  fire. /automation scheduler on turns it on.`; off with nothing enabled → nothing.
- Empty: `No automations yet. In a mini-ork thread, ask to run a recipe on a schedule
  ("run changelog-entry every weekday at 9"), or use mini-ork automations add.`
- Card: heading `### <mark> <name>` then a line `` `<id>` · <recipe> · <when> (`<cron>`) ·
  <in a new worktree | in place> · <enabled | paused> ``; `Next: <3 times>` (or `Paused.`);
  the kickoff in a fenced ```` ```markdown ```` block (first 40 lines, `…` when longer); a
  `Recent runs` table `| | run | started | status |` from `runs` (≤ 10; status and mark from
  the same history call, `—` for runs not in the history); `Last firing failed: <error>`
  when `last_error`; a last line with the commands: `` `/automation run <id>` ·
  `/automation pause <id>` (or `resume`) · `/automation delete <id>` ``. Unknown id → None.

### Commands (`commands.py`)

Handlers (keys in `HANDLERS`; the dispatcher already matches the longest key):

- `automations` → `render_automations(home)`.
- `automation` → `<id>` → card (unknown → "No automation <id>. `/automations` lists
  them."); empty arg → the table.
- `automation run <id>` → `automations.fire(home, id)`; ok → "Started <run id> for <name>
  — <in worktree `mini-ork/<run id>` | in place>. It is in the thread list; `/status <run
  id>` shows it."; error → "Could not start <id>: <error>".
- `automation pause <id>` / `automation resume <id>` → "Paused <name> — it will not fire
  until `/automation resume <id>`." / "Resumed <name> — next run <time>."
- `automation delete <id>` → `remove`; "Deleted <name>. Its past runs stay in `/runs`."
- `automation scheduler` (bare or `status`) → one paragraph: on/off, the command it runs,
  the last tick, the tick log path. `automation scheduler on` → `install_scheduler(home)`;
  "Scheduler on — automations in this project fire even when Zed is closed (a LaunchAgent
  / crontab line ticks every minute). `/automation scheduler off` removes it."; error →
  "Could not turn the scheduler on: <error>". `off` → `uninstall_scheduler`.
- Missing id for run/pause/resume/delete → "Which automation? `/automations` lists them."
- Announce `automations` ("Scheduled recipe runs: when, next, last run, scheduler.") and
  `automation` ("Automation card, or run|pause|resume|delete <id>, scheduler on|off.").
- `/merge` (command) uses the same commit message as the S5 button (see
  `_handle_review_decision` in `agent.py`, which is NOT in scope — do not edit it):
  `base = agent._thread_titles.get(session_id) or agent._run_base_titles.get(run_id) or ""`
  (read both with `getattr(agent, "…", {})`), message `f"{base} (mini-ork {run_id})"` if
  `base` else `f"mini-ork run {run_id}"`. Put that in a small module-level helper in
  `commands.py`.

## Tests

`tests/unit/test_automations.py`: `next_fires` (3 times, bad expr → []); `propose` writes
only the draft and validates like `add` (bad cron, unknown recipe, empty kickoff → nothing
written); `propose` on an existing id → `exists: true`; `commit_proposal` adds (new) and
updates (existing; `enabled`, `runs` kept); proposal file gone after commit; no proposal →
error; `discard_proposal`.

`tests/unit/test_mcp_context_server.py`: `propose_automation` only with control; it writes a
proposal and never touches `automations.json`; `list_automations` lists with `when`,
`next_fire` (null when paused) and `scheduler`; no add/fire/delete tool exists.

`tests/unit/test_acp_automation_view.py` (temp home with a state DB holding run rows, as
the `recipe_view` tests build them; fixed `now`): table marks (⏸, ·, ✓ from a published
run, ✗ from `last_error`), `next` formats (today / tomorrow / weekday), footer for each
scheduler state (`scheduler_status` monkeypatched), empty text; card fields, kickoff
truncation, recent runs table, unknown id → None.

`tests/unit/test_acp_commands.py`: `/automations`; `/automation <id>`; `run` (fire
monkeypatched → message, error message); `pause` / `resume` / `delete`; `scheduler`
status/on/off with the `automations` scheduler functions monkeypatched (never the real
launchd/crontab); missing id; `/merge` message uses the title.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_automations.py tests/unit/test_mcp_context_server.py tests/unit/test_acp_automation_view.py tests/unit/test_acp_commands.py tests/unit/test_acp_agent_py.py` passes.
- `ruff check` clean; diff touches only the files in scope.
