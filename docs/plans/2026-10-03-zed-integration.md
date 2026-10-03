# Everything a mini-ork user needs, inside Zed

Status: in progress (2026-10-03). Owner surface: `mini-ork acp` (`mini_ork/acp/`).

## Goal

Anything of value to a mini-ork user is reachable from Zed's agent panel, without a Zed
fork: start runs, see run history, attach to any run that is already going (including
ones started from the CLI), watch each agent's output live, see diffs, read learnings,
check cost and lanes, certify a change, stop / resume / recover runs.

## Why no fork

Zed's External Agent surface already carries all of it through the Agent Client
Protocol (ACP, `agent-client-protocol` 0.12): Thread History + Import Threads call
`session/list` and `session/load`; slash commands come from `available_commands_update`;
plans, thought chunks, tool calls with text / diff content, usage and permission prompts
are all first-class. What ACP cannot do is draw custom panels (a DAG graph, dashboards);
those stay in `mini-ork serve` (web UI) and are linked from Zed.

Verified 2026-10-03: a real Zed thread drives `mini-ork acp`, node tool calls stream into
the thread, and the turn ends with the run's verdict.

## Feature → Zed surface

| mini-ork feature | Zed surface | Slice |
|---|---|---|
| Start a run in the open project | new thread → prompt | done (Slice 0) |
| Run home = the project Zed has open (not the agent's process cwd) | `session/new` / `load` `cwd` | Z1 |
| Run history (every run of the project, incl. CLI-started) | Thread History → Import Threads (`session/list`) | Z2 |
| Open a past run: kickoff, plan, node calls, verdict, cost | `session/load` replay | Z2 |
| Attach to a run that is still going | `session/load` replay + live follow until terminal | Z2 |
| Each agent's live output (text, thinking, commands) | `tool_call_update` content + `agent_thought_chunk` | Z3 |
| What the run changed | `diff` tool content → Review Changes | Z4 |
| Learnings, cost, lanes, status, stop, resume, recover, certify, runs | slash commands (`available_commands_update`) | Z5 |
| Pick the recipe (code-fix, docs, research, audit, …) | session modes / config options | Z6 |
| The run's DAG as a live checklist | `plan` updates (entries = nodes) | Z6 |
| Learnings / run data for other agents in Zed | MCP server `mini-ork-mcp-context` (Zed forwards MCP) | Z7 |
| One-click install + setup | ACP Registry entry, `mini-ork zed setup`, docs/ZED.md | Z8 |
| First-run check: project, orchestrator login, lane keys | ACP terminal auth → `mini-ork acp --setup` | Z10 |
| DAG graph, dashboards | `mini-ork serve` (web UI), linked by `/serve` | Z5 |
| Talk to an orchestrator that drives mini-ork (like a coding assistant in a terminal) | thread = orchestrator conversation; mode/model pickers via session config options | Z9a–c |
| Orchestrator threads in history, reopened with the conversation resumed | `session/list` + `session/load` of `orch-` threads | Z9c-2 |

## Slices (serial: each one edits `mini_ork/acp/agent.py`)

- **Z1+Z2 — home from session cwd, history, attach.** `mini_ork/acp/history.py` builds
  `SessionInfo` rows and replay updates from the read model
  (`mini_ork/web/repositories.py`); `agent.py` advertises `load_session` +
  `session_capabilities.list`, implements `list_sessions` / `load_session`, and follows
  an in-progress run to its end after replaying it.
- **Z3 — live agent output.** `mini_ork/acp/live.py` tails `agent-<node>.live.jsonl`
  (byte offsets, like `web/routes/node_live.py`), normalizes claude stream-json and
  codex item events into text / thought / command chunks under that node's tool call.
- **Z4 — diffs.** When an implementer node ends, emit `diff` content for each changed
  file (old text from the pre-implementer ref, new text from the tree).
- **Z5 — slash commands.** `mini_ork/acp/commands.py`: `/runs`, `/status`, `/learnings`,
  `/cost`, `/lanes`, `/stop`, `/resume`, `/recover`, `/certify`, `/serve`, `/help`.
- **Z6 — recipe modes + live plan.** Recipes as session modes; plan entries per node.
- **Z7 — MCP context server.**
- **Z8 — packaging and docs.**
- **Z9a — MCP control tools.** `mcp-context --control`: list_recipes, start_run,
  run_status, wait_for_run, stop_run, certify.
- **Z9b — orchestrator harness.** `mini_ork/acp_orchestrator/`: one claude-CLI turn
  on a chosen lane with the control tools; default lane opus, configurable.
- **Z9c-1 — the thread is the orchestrator.** Mode/model/recipe config options;
  runs started in a thread stream into it under `<run_id>:` tool ids; one
  thread cost.
- **Z9c-2 — thread persistence.** `<home>/acp-threads/<id>.jsonl`; list, replay,
  resume.

- **Z10 — first-run setup.** `mini-ork acp --setup`, advertised as an ACP terminal
  auth method; a thread whose orchestrator cannot run asks for it.

Status (2026-10-03): every slice above is merged (Z6's recipe picker shipped as the
Recipe config option in Z9c-1). Left: the ACP Registry listing, which needs a PyPI
release.

Fixed along the way: a docs-recipe run with nothing to do ended "published"
(observed from Zed with the prompt "hi"); it now ends without a publish (ed17ffee).
