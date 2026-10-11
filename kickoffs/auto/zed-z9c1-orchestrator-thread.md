# Zed Z9c-1 — the thread is an orchestrator conversation (model + mode pickers, child runs)

Plan: `docs/plans/2026-10-03-zed-integration.md`. Depends on Z2, Z3, Z9a, Z9b merged.

## Goal

Today every Zed prompt becomes one mini-ork run (session id == run id). The user wants
the text bar to talk to an **orchestrator** — an LLM agent that converses, reads the repo,
consults history, writes kickoffs, launches and follows runs and reports back — with the
model picked like other Zed agents do it: ACP **session config options** (`category:
"model"` renders as Zed's model picker, `category: "mode"` as its mode picker). Default
model `opus` (Claude subscription), user-configurable (`mini_ork.acp_orchestrator.config`).

## Files in scope

- `mini_ork/acp/agent.py`
- `mini_ork/acp/orchestration.py` — NEW: stream-json → ACP update mapping for the
  orchestrator's own events, and child-run detection.
- `tests/unit/test_acp_orchestration.py` — NEW.
- `tests/unit/test_acp_agent_py.py` — extend; existing tests keep passing (adjust only
  tests that assert session id == run id for NEW sessions, and say so in a comment).

No other file changes.

## Mechanism (exact spec)

### Session kinds

- **Thread sessions** (new): `session/new` mints `orch-<epoch>-<6 hex>`; the session holds
  config (mode, model, recipe), the claude session id to resume, and its child runs.
- **Run sessions** (unchanged): ids that are run ids, created by `session/load` of a run
  (Z2). Their behaviour stays exactly as today.

### Config options (returned by `session/new` and by `set_config_option`)

Build with the SDK's `SessionConfigOptionSelect` / `SessionConfigSelectOption` types
(introspect `acp.schema`; values are strings), in this order:
1. `mode` — `category="mode"`, values `orchestrate` ("Orchestrate — talk to the mini-ork
   orchestrator") and `direct` ("Direct run — each prompt is a run kickoff"); default
   `MO_ACP_DEFAULT_MODE` or `orchestrate`.
2. `model` — `category="model"`, values = `orchestrator_lanes()` (`id` → value, `name` →
   label); default `default_lane(home)`.
3. `recipe` — no category, values = recipes (reuse the Z9a `list_recipes` logic or
   `mini_ork.acp.modes` if present; recipe ids sorted); default `MO_ACP_RECIPE` or
   `code-fix`; used by direct mode only.
Also return the legacy `modes` (`SessionModeState`) mirroring the `mode` option, and keep
both in sync.

`async def set_config_option(self, config_id, session_id, value, **kw)`: validate id and
value (invalid → invalid params), store, emit `ConfigOptionUpdate` (session update
`config_option_update` with the full list) and return the response type carrying the full
list. `set_session_mode(mode_id)` maps onto the `mode` option.

### Prompt in `orchestrate` mode

- A slash command (`/run <task>`) is handled first: it behaves like direct mode for that
  one prompt.
- Otherwise call `mini_ork.acp_orchestrator.harness.run_turn(lane=<model>, prompt=<text>,
  cwd=<session cwd>, home=self._home_for(session_id), resume=<stored claude session id>,
  on_event=...)` (injectable seam `orchestrator_turn=` on the constructor for tests).
- `orchestration.py` maps each stream-json event (claude CLI format):
  - assistant `text` block → `AgentMessageChunk`;
  - assistant `thinking` block → `AgentThoughtChunk`;
  - assistant `tool_use` block → `ToolCallStart(tool_call_id=<tool_use id>, title=<tool
    name without the `mcp__mini-ork__` prefix> + short arg summary, status="in_progress",
    kind="read" for Read/Grep/Glob/LS else "other", raw_input=<input>)`;
  - user `tool_result` block → `ToolCallProgress(tool_call_id=<tool_use_id>,
    status="completed" or "failed" when is_error, content=[text ≤ 2000 chars])`;
  - everything else ignored.
- **Child runs**: when a `tool_result` answers a `start_run` tool_use and its JSON text
  carries a `run_id`, start following that run in this thread (reuse the Z2/Z3 projection:
  lifecycle tool calls + live agent output) with tool call ids prefixed `"<run_id>:"` so
  they never collide with the orchestrator's own tool ids; add a `ToolCallStart` titled
  `run <run_id> (<recipe>)` as the parent marker. Followers stop at the run's terminal
  status or on cancel; they keep emitting after the orchestrator's turn ends only while
  the run is going.
- Store the returned claude `session_id` for the next turn's `--resume`. Turn result rc
  != 0 → agent message with the error tail, `stop_reason="end_turn"`.
- `cancel` → cancel the running turn task (the harness kills its process) and stop
  followers; child runs keep running (stop them with `/stop` later — not in this slice).

### Prompt in `direct` mode

Launch a run with the session's `recipe` (existing launcher seam, a fresh run id) and
follow it as a child run (same projection as above) until terminal; then `end_turn`.

## Tests

`tests/unit/test_acp_orchestration.py`: mapping of each stream-json block type; title
prefix stripping; failed tool_result; `start_run` result → run id extracted (and ignored
for other tools / non-JSON text).

`tests/unit/test_acp_agent_py.py` (fake `orchestrator_turn` that calls `on_event` with
canned events, fake launcher/reader as in existing tests):
- `new_session` returns the three config options (+ modes) with defaults; ids start with
  `orch-`.
- `set_config_option` for model/mode/recipe stores and returns the full list; invalid →
  error; `config_option_update` emitted.
- orchestrate prompt streams message/thought/tool-call updates, stores the resume id,
  passes it on the next turn, uses the chosen model lane.
- a `start_run` tool_result makes the thread follow that run (prefixed tool ids, parent
  marker, terminal stops follower).
- direct mode launches with the selected recipe and follows the run.
- `/run fix x` in orchestrate mode launches directly.
- run sessions loaded via `session/load` behave exactly as before.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_acp_orchestration.py tests/unit/test_acp_agent_py.py tests/unit/test_acp_history.py tests/unit/test_acp_live.py tests/unit/test_acp_orchestrator.py` passes.
- `ruff check` clean; diff touches only the files in scope.
- No test launches a real run or a real claude process (the module's autouse guard
  `_never_launch_a_real_run` must stay and keep passing).
