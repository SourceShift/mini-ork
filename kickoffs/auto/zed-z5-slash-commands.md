# Zed Z5 — slash commands: runs, status, learnings, cost, lanes, stop, resume, recover, certify, serve

Plan: `docs/plans/2026-10-03-zed-integration.md`. Depends on Z2, Z3, Z9c-1/2 and Z4 merged
(serial: every Zed slice edits `mini_ork/acp/agent.py`).

## Goal

From a mini-ork thread in Zed, the user can ask mini-ork things and control runs without
leaving the editor. ACP carries slash commands: the agent announces them with an
`available_commands_update`, Zed shows them when the user types `/`, and the command
arrives as the prompt text. A command must never launch a run.

Two session kinds exist (read `mini_ork/acp/agent.py` first): **thread** sessions
(`orch-…`, an orchestrator conversation that starts runs — `_thread_sessions`, runs it
follows are in `_routes` as `run_id → (thread_id, prefix)`) and **run** sessions (the
session id is a run id, opened from history). `/run <task>` already exists for threads
(`_strip_slash_run`) and must keep working.

## Files in scope

- `mini_ork/acp/commands.py` — NEW: command table + handlers.
- `mini_ork/acp/agent.py` — announce commands, route `/…` prompts to the handler.
- `tests/unit/test_acp_commands.py` — NEW.
- `tests/unit/test_acp_agent_py.py` — extend.

No other file changes.

## Mechanism (exact spec)

### Announcement

After `session/new` and after `session/load` (once the replay is emitted), emit
`AvailableCommandsUpdate(session_update="available_commands_update", available_commands=COMMANDS)`.
`COMMANDS` is a list of `AvailableCommand(name=..., description=..., input=AvailableCommandInput(...hint...) when the command takes an argument)`
— build `input` with whatever constructor the installed SDK exposes for an unstructured
hint (introspect `acp.schema.AvailableCommandInput`).

### Routing in `prompt`

If the prompt text, stripped, starts with `/` and its first token (without `/`) is a known
command other than `run`: call `await commands.handle(self, session_id, name, arg_text)`, emit the returned
markdown as one `AgentMessageChunk`, return `PromptResponse(stop_reason="end_turn")`.
Never call the launcher for a command. An unknown `/word` → reply "Unknown command /word —
/help lists commands." and end the turn (do not launch). This applies to new and loaded
sessions alike.

### Handlers (`commands.py`)

Each handler returns markdown; failures return a one-line explanation, never raise. Use
`agent._home_for(session_id)` for the home and `mini_ork.web.deps.db_for(home)` for the DB.
"Current run": in a run session, the session id. In a thread, the thread's most
recent run — keep `self._thread_runs: dict[thread_id, list[run_id]]`, appended when a run
is followed in the thread (`_start_child_follow`, direct mode) and rebuilt on thread load
from the replayed run markers. Commands that act on a run (`/status`, `/stop`, `/kill`,
`/resume`, `/recover`) accept an optional run id as the first argument, which wins. A
thread with no run yet → "No run in this thread yet — /runs lists the project's runs." If
the run has no task_run row yet, say so.

| command | argument | behaviour |
|---|---|---|
| `/help` | — | list every command with its description, including `/run <task>` (threads: start the selected recipe directly). |
| `/runs` | optional count (default 10, max 50) | newest-first table: run id, status, recipe, cost (`$0.00`), age, title — via `mini_ork.acp.history.list_runs`. End with: "Open any of these from Thread History → Import Threads." |
| `/status` | — | current run: status, recipe, nodes (from lifecycle events: done / running / failed), cost so far (sum of llm_calls via `history.read_snapshot`), run dir path. |
| `/learnings` | optional text filter | failure-mode gradients for the current run's task class (`RunDetailRepository.fetch_failure_mode_gradients(task_class)`, task class from the task_run row), the current run's learning records (`fetch_learning_records(run_id)`), and the top emergent patterns (`mini_ork.web.routes.learning.emergent_patterns(db, 10)`); filter every list by the text (case-insensitive) when given; cap 10 items per section; say "none recorded yet" for empty sections. |
| `/cost` | optional days (default 1) | spend per recipe per day over the window (`mini_ork.web.routes.trajectory.cost_by_day(db)`), total, this run's cost, and the daily cap `MO_DAILY_BUDGET_USD` (default 50) with what remains in the rolling 24h — spend from `mini_ork.cost_ledger.spent_last_24h(<db path>)` (the ledger the budget guard reads). |
| `/lanes` | — | role → lane map (`mini_ork.web.recipes.load_lanes(home)`). |
| `/stop` | — | soft-stop the current run (`mini_ork.web.control.stop_run(home, db, run_id)`), report the result. |
| `/kill` | — | hard-kill (`control.kill_run`). |
| `/resume` | — | clear a cost pause (`control.resume_cost_run(home, run_id, approver="zed")`); report "no cost pause on this run" when ok is False. |
| `/recover` | optional `--from-node <id>` | start `mini-ork recover <run_id> [--from-node …]` DETACHED with the same spawn shape as `control.launch_run` (`[sys.executable, <root>/bin/mini-ork, "recover", run_id, ...]`, cwd = engine root, env with `MINI_ORK_HOME`, `MINI_ORK_ROOT`, minus `MINI_ORK_VENV_ACTIVE`, stdout/stderr to `<home>/runs-inbox/<run_id>.recover.log`, `start_new_session=True`); reply with the pid and log path; if the session is loaded/attached, the existing follower picks up the new node events. |
| `/certify` | the bug report text (required) | run `[sys.executable, <root>/bin/mini-ork, "certify", "--repo", <session cwd>, "--issue", <text>]` (defaults: base HEAD~1, head HEAD) with a timeout `MO_ACP_CERTIFY_TIMEOUT_S` (default 900); before starting, emit an agent message "Certifying HEAD~1..HEAD — this takes 1–3 minutes."; reply with certify's summary lines (its stdout, first 20 lines) and the verdict word from the exit code (0 PROVEN, 1 REFUTED, 2 UNVERIFIED, other → error + stderr tail). Missing text → usage line. |
| `/serve` | — | probe `http://127.0.0.1:${MO_SERVE_PORT:-7090}/health` (1s timeout, `urllib`); reply with the run's URL (`http://127.0.0.1:<port>/runs/<run_id>`) when up, else the command `mini-ork serve` and that it is not running. Never start it. |

Every subprocess / network call goes through a module-level seam
(`_spawn`, `_run`, `_probe`) so tests stay hermetic.

## Tests

`tests/unit/test_acp_commands.py`: one test per command against a temp home with a
migrated DB and seeded rows (runs, llm_calls, a gradient, a learning record); seams
monkeypatched for `/recover`, `/certify`, `/serve`; filters and caps; empty-data replies;
handler errors produce a one-line message instead of raising.

`tests/unit/test_acp_agent_py.py` (no pytest-asyncio — drive with `asyncio.run`; keep the
autouse `_never_launch_a_real_run` guard): `available_commands_update` is emitted after
`new_session` and after `load_session` (run and thread); a `/status` prompt in a run
session and in a thread returns `end_turn`, never calls the launcher or the orchestrator
turn; in a thread `/status` reports the thread's latest run and `/status <run_id>` the
named one; a thread with no run says so; `/run fix x` still launches directly; an unknown
`/foo` is answered without launching; a normal prompt still goes to the orchestrator.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_commands.py tests/unit/test_acp_agent_py.py tests/unit/test_acp_threads.py tests/unit/test_acp_diffs.py tests/unit/test_acp_history.py tests/unit/test_acp_live.py` passes.
- `ruff check` clean on touched files.
- Diff touches only the files in scope.
