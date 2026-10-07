# Agent sessions run with mini-ork's settings, not the operator's personal Claude Code setup

## Why (live evidence, 2026-10-07)

Every claude-CLI node (planner, lenses, implementer, reviewer, gradient-extract, pattern-induct,
rubric) loads the operator's USER-level Claude Code config (`~/.claude/settings.json`,
`~/.claude/CLAUDE.md`, user plugins). That config is written for the operator's own interactive
sessions:

- The reviewer stream of `runs/files-abs-resolve-20261007191620` shows **14 SessionStart hooks**.
  They inject "You are in 'learning' output style mode" (it tells the agent to ask the human to
  write code), "explanatory" mode and "CAVEMAN MODE". The init event lists **190 skills,
  43 agents, 240 slash commands and 15 plugins**.
- The user CLAUDE.md asks every response to end with a `<z-insight>` JSON block for a personal
  dashboard. Over the last 24 h, **37% of all agent final text (240K of 644K chars)** was
  `<z-insight>` blocks; 64 of 330 agent logs contain one. These are expensive output tokens.
  Stop hooks also ship each agent session to the personal ContextNest and z-dashboard, which then
  feed back into planner prompts as "attention inbox" items.
- Probe (claude 2.1.283, `claude -p "Reply with exactly: ok"`):
  - default: 7 hooks, 25.6K cache-create + 17.4K cache-read tokens;
  - `--setting-sources project,local`: 0 hooks, 18 skills, 11.7K + 17.4K;
  - the z-insight rule is gone, while the project CLAUDE.md (mini-ork) still loads.
  That is about 14K fewer tokens on EVERY turn of EVERY node.

The ACP orchestrator already fixed this for its own thread:
`mini_ork/acp_orchestrator/harness.py:150-158` adds `--setting-sources` (env
`MO_ORCHESTRATOR_SETTING_SOURCES`, default `project,local`) and `--model` for subscription lanes.
Node dispatch never got the same fix.

Two user settings DO shape today's runs and must be kept explicitly, not lost:

1. `model: claude-opus-5-5`: the `opus` and `sonnet` lanes (`config/providers.yaml`,
   `kind: anthropic-native`, no `model`) get their model ONLY from it.
2. `effortLevel: xhigh` + `alwaysThinkingEnabled: true`: today's reasoning depth for all
   claude-CLI nodes. Lanes with `CLAUDE_CODE_EFFORT_LEVEL` in `extra_env` (deepseek) already
   override it, and the rubric sets its own.

The `rtk hook claude` PreToolUse(Bash) user hook compacts command output for agents today. Keep it
(conditional on `rtk` being installed).

## Files in scope (touch ONLY these)

- `mini_ork/dispatch/providers.py`: a new helper + `_claude_command_builder` only
- `mini_ork/gates/rubric_prescreen.py`: ONLY the `cmd = ["claude", "-p", …]` construction (~:427)
- `mini_ork/recovery/cleaner.py`: ONLY the `["claude", "-p", …]` argv (~:92)
- `config/agent-claude-settings.json` (new)
- `tests/unit/test_agent_session_isolation.py` (new)

Do NOT modify any other file. Leave `acp_orchestrator/harness.py` as is (it has its own flag).

## Changes (exact)

1. **`config/agent-claude-settings.json`:**
   ```json
   {
     "effortLevel": "xhigh",
     "alwaysThinkingEnabled": true,
     "hooks": {
       "PreToolUse": [
         {"matcher": "Bash", "hooks": [{"type": "command",
           "command": "if command -v rtk >/dev/null 2>&1; then exec rtk hook claude; fi; cat >/dev/null"}]}
       ]
     }
   }
   ```
2. **`providers.claude_isolation_args(env, lane=None, argv=()) -> list[str]`** (new, public, pure):
   - `MO_NODE_SETTING_SOURCES` (default `"project,local"`). Set to empty (`""`) to return `[]`:
     legacy behaviour, everything loads.
   - Otherwise returns `["--setting-sources", v, "--settings", <abs path of
     config/agent-claude-settings.json, resolved from the package location, not cwd>]`.
     `MO_NODE_SETTINGS` overrides the path (empty = omit `--settings`).
   - Plus `["--model", lane]` when `lane` is `"opus"` or `"sonnet"`, `--model` is not in `argv`
     and `ANTHROPIC_MODEL` is not in `env`. This mirrors `_SUBSCRIPTION_MODEL_ALIASES` in the ACP
     harness.
   - Never duplicates a flag already present in `argv`.
3. **`_claude_command_builder`:** for a `claude` argv, insert `claude_isolation_args(env,
   request.model, command)` right before `--output-format` (append when absent). Apply it
   regardless of `MO_TOOL_GRANTS_DISABLED`, before the tool grants. Grant and resume behaviour stay
   the same.
4. **The rubric and the cleaner** add `*claude_isolation_args(<their env>, <lane/model>, cmd)`
   to their argv. Their existing `--model`, effort and budget flags stay.

## Tests (`tests/unit/test_agent_session_isolation.py`)

- **Defaults:** the builder on a claude argv for lane `glm` contains `--setting-sources
  project,local` and `--settings <…/config/agent-claude-settings.json>` (the file exists and is
  valid JSON with `effortLevel` and the PreToolUse Bash hook), positioned before `--output-format`.
- **Lane `opus`:** `--model opus` is added. With `ANTHROPIC_MODEL` in env, or `--model` already in
  argv, it is not.
- **`MO_NODE_SETTING_SOURCES=""`:** argv unchanged by the isolation step (grants still applied).
- **No duplicates:** an argv that already has `--setting-sources` is not changed.
- **Non-claude argv** (e.g. `codex`) is untouched.
- **The rubric argv** (monkeypatch `subprocess.run` and capture) includes `--setting-sources`.
  Same for the cleaner's worker argv.

## Verification command

The command that proves this run succeeded (per file; the host kills one CPU-bound process
that runs longer than 30 s):

```bash
for f in tests/unit/test_agent_session_isolation.py tests/unit/test_tool_grants_py.py tests/unit/test_rubric_prescreen_py.py tests/unit/test_cleaner_py.py tests/unit/test_providers_registry.py; do env -u MINI_ORK_RUN_ID -u MINI_ORK_DB -u MINI_ORK_HOME python3.11 -m pytest -q -p no:asyncio "$f" || exit 1; sleep 3; done   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary lines.
- `uvx ruff check mini_ork/dispatch/providers.py mini_ork/gates/rubric_prescreen.py mini_ork/recovery/cleaner.py tests/unit/test_agent_session_isolation.py` → clean.
- **Live proof:** build the real argv for lane `glm` through `_claude_command_builder` and run it
  from this worktree with the prompt "Reply with exactly: ok"
  (`--output-format stream-json --verbose`, `--max-turns 1`). Parse the init event and the
  result. Paste the counts of `skills` / `plugins` / `hook_response` events and the usage
  (expect 0 hooks besides the rtk one, and fewer cache-create tokens than without the flags).
- `git diff --stat` touches only the files in scope.
