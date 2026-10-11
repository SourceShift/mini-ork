# Zed Z9b — orchestrator harness: one conversational turn on a chosen lane, with mini-ork tools

Plan: `docs/plans/2026-10-03-zed-integration.md` (orchestrator section).

## Goal

Zed's text bar will talk to a mini-ork **orchestrator**: an LLM agent that converses with
the user, reads the repo, consults past runs and learnings, writes kickoffs, launches and
watches mini-ork runs, and reports back — the way a coding assistant drives mini-ork from
a terminal. This slice builds the engine-agnostic core the ACP agent will call (wiring
into `mini_ork/acp/agent.py` is a later slice): pick the lane, build the command, run one
turn, stream its events, keep the conversation resumable.

The user chose: default lane `opus` (their Claude subscription), user-configurable — in
mini-ork's own config like other Zed agents own their native config, and per thread from
Zed's model picker (later slice).

## Files in scope

- `mini_ork/acp_orchestrator/__init__.py` — NEW.
- `mini_ork/acp_orchestrator/config.py` — NEW: lane choices + default.
- `mini_ork/acp_orchestrator/harness.py` — NEW: one turn.
- `mini_ork/acp_orchestrator/prompt.md` — NEW: the orchestrator's system prompt.
- `tests/unit/test_acp_orchestrator.py` — NEW.

No other file changes.

## Mechanism (exact spec)

### `config.py`

```python
def orchestrator_lanes(root: Path | None = None) -> list[dict]   # [{"id", "name"}]
def default_lane(home: Path | None) -> str
```

- `orchestrator_lanes`: every lane the provider registry resolves (`mini_ork.dispatch.providers.resolve_provider`
  over the registry's lane names — find the function that lists them; read
  `providers.py` before coding) whose `command[0]` is `claude` (the harness drives the
  claude CLI). `name`: `opus` → "Opus (Claude subscription)", `sonnet` → "Sonnet (Claude
  subscription)", otherwise the lane's `model:` field from providers.yaml when it is a
  real model name (e.g. "MiniMax-M3", "GLM-5.3", "deepseek-v4-pro[1m]"), else the lane id.
  Order: `opus` first, then `sonnet` if present, then the rest sorted.
- `default_lane`: `MO_ORCHESTRATOR_LANE` env → the `orchestrator` role in the home's lane
  map (`mini_ork.web.recipes.load_lanes(home).get("orchestrator")`, which already honours
  the per-user agents overlay) → `"opus"`. Unknown/unresolvable value → `"opus"` with a
  stderr warning.

### `harness.py`

```python
@dataclass
class TurnResult:
    session_id: str | None   # the claude CLI session id (for --resume next turn)
    rc: int
    text: str                # final result text
    cost_usd: float
    error: str = ""

async def run_turn(*, lane: str, prompt: str, cwd: Path, home: Path, resume: str | None,
                   on_event: Callable[[dict], Awaitable[None]], timeout_s: float = 3600,
                   launcher: str | None = None) -> TurnResult
def build_command(lane: str, *, resume: str | None, mcp_config_path: Path,
                  prompt_path: Path) -> tuple[list[str], dict[str, str]]
```

- `build_command`: start from `resolve_provider(lane)` (`command`, `env`, model handling —
  reuse exactly how the dispatch layer adds `--model` for that lane kind; do not
  re-derive credentials). Replace `--output-format json` with
  `--output-format stream-json --verbose`; replace `--permission-mode bypassPermissions`
  with `--permission-mode default`; add `--mcp-config <mcp_config_path>`,
  `--strict-mcp-config`, `--allowedTools mcp__mini-ork__* Read Grep Glob LS`,
  `--disallowedTools Edit Write MultiEdit NotebookEdit Bash`,
  `--append-system-prompt-file <prompt_path>`, and `--resume <resume>` when given. The
  user prompt goes on stdin (as the dispatch layer does), never argv.
- `run_turn`: write a temp MCP config JSON
  `{"mcpServers": {"mini-ork": {"command": <launcher>, "args": ["mcp-context", "--control"], "env": {"MINI_ORK_HOME": str(home)}}}}`
  (`launcher` = absolute `mini-ork` path: `shutil.which("mini-ork")`, else
  `~/.local/bin/mini-ork`, else `<root>/bin/mini-ork`); spawn the command with
  `asyncio.create_subprocess_exec`, cwd=`cwd`, env = `os.environ` + lane env minus
  `MINI_ORK_VENV_ACTIVE`, stdin=prompt; read stdout line by line, `json.loads` each,
  `await on_event(obj)` for every parsed object (skip non-JSON); from the final
  `{"type":"result"}` object take `session_id`, `result` text and `total_cost_usd`
  (pricing: call `mini_ork.dispatch.providers.claude_cost(stdout_so_far, None)` on the
  collected lines so non-Anthropic lanes are priced at list price); timeout → kill the
  process group, `rc=124`, `error="timeout"`; spawn failure → `rc=127`. Always delete the
  temp MCP config. stderr is captured and its tail goes into `error` on rc != 0.
- Cancellation: if the awaiting task is cancelled, terminate the process (SIGTERM, then
  SIGKILL after 3s) and re-raise.

### `prompt.md` (system prompt, plain and specific)

The orchestrator: talks with the user; reads the repo with Read/Grep/Glob/LS only (never
edits — all changes go through mini-ork runs); before launching, checks `learnings` and
`list_runs` for relevant history; picks a recipe with `list_recipes` (code-fix for code
changes, docs for documentation, research/audit recipes for analysis); writes a kickoff
with: a one-paragraph goal, a "## Files in scope" list of real paths, exact success
criteria/commands, and constraints; launches with `start_run`; follows with
`wait_for_run` (in a loop for long runs, telling the user what is happening between
waits); on completion reads `run_status`/`run_detail` and reports in plain language what
changed, whether verification passed, cost, and next steps; offers `certify` for a
change the user wants proven; uses `stop_run` only when the user asks; asks a short
clarifying question when the request is ambiguous instead of guessing; keeps replies
short.

## Tests (`tests/unit/test_acp_orchestrator.py`)

- `orchestrator_lanes` with `resolve_provider` / registry monkeypatched: claude-family
  only, opus first, names as specified.
- `default_lane` precedence: env > lane map `orchestrator` role > "opus"; unknown → opus.
- `build_command` for a subscription lane and an anthropic-compat lane: output format,
  permission mode, MCP flags, allowed/disallowed tools, prompt file, `--resume` present
  only when given, env carries the lane's env.
- `run_turn` against a fake `claude` (a temp executable script on PATH, or monkeypatched
  `build_command`) that prints three stream-json lines (assistant text, tool_use, result
  with session_id/cost): `on_event` receives all three in order; `TurnResult` has the
  session id, text, cost; the temp MCP config is removed; non-JSON lines skipped; a fake
  that sleeps past a tiny timeout → rc 124; a missing binary → rc 127.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_acp_orchestrator.py` passes.
- `ruff check` clean; diff touches only the files in scope.
