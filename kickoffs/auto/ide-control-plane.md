# The mini-ork thread is a control plane: no pickers, direct dev allowed, images attach

## Goal

In Zed the mini-ork thread (the ACP agent, `mini-ork acp`) shows four pickers under
the composer: Mode (Orchestrate / Direct run), Model, Recipe, Workspace ("New worktree
per task"). The user wants the thread to be a plain control plane: they say what they
want; the orchestrator decides — or asks in the chat when it matters — whether to
answer, start a verified mini-ork run (choosing recipe and worktree vs in place), or do
the work directly as an agentic developer. Images can't be attached today; fix that.

## Files in scope

- `mini_ork/acp/agent.py`
- `mini_ork/acp_orchestrator/harness.py`
- `mini_ork/acp_orchestrator/prompt.md`
- `mini_ork/ide_pages/orch.py` (its "Thread defaults" section lists the removed pickers)
- tests: `tests/unit/test_acp_agent_py.py`, `tests/unit/test_ide_pages_orch.py`, and
  the orchestrator harness tests (`grep -rln _ALLOWED_TOOLS tests`)

No other file changes.

## Changes (exact)

1. **Only the model picker.** `_build_config_options` returns just the model option
   (`category="model"`). Remove the mode/recipe/workspace options and
   `_build_session_modes` from new and loaded sessions (no `modes` in the
   new/load/resume responses). `set_config_option` / `set_mode` still accept the old
   ids (`mode`, `recipe`, `workspace`) from older clients and store them, but they are
   no longer offered. Every prompt goes to the orchestrator; the `/run`, `/race`,
   `/kickoff`, … slash commands keep working exactly as today (direct runs stay
   reachable through `/run`). Stored recipe/workspace values become the defaults the
   orchestrator proposes.
2. **Direct agentic dev.** In `harness.py` allow `Edit`, `Write`, `MultiEdit`, `Bash`
   (keep `NotebookEdit` disallowed) in addition to today's tools; they act in the
   thread's working directory (unchanged cwd). Update the comments that say the
   orchestrator must never edit.
3. **Orchestrator prompt** (`prompt.md`): rewrite the role section. The orchestrator is
   the project's control plane and chooses per request:
   - answer / inspect (read tools, mini-ork lookups) — default for questions;
   - start a mini-ork run (`start_run`) for features and fixes that deserve
     verification and review — propose the recipe and worktree-vs-in-place, and ask
     one short question when it is genuinely unclear (otherwise use the thread
     defaults); report progress with `wait_for_run`;
   - edit directly (Edit/Write/Bash) when the user asks for hands-on work or the change
     is small and explicit; say what it changed and suggest a run when verification
     would help.
   Attached images arrive as file paths in the prompt; read them with `Read`.
   Keep the existing reporting/cost/clarifying-question guidance.
4. **Images and attachments.** `initialize` advertises
   `agent_capabilities.prompt_capabilities` with `image=True` and
   `embedded_context=True`. Replace `_extract_prompt_text` use in the orchestrate and
   direct paths with a helper that also handles non-text blocks: an image block
   (base64 `data` + `mime_type`) is written to
   `<home>/attachments/<session_id>/<n>.<ext>` (ext from the mime type) and the prompt
   gains a line `Attached image: <absolute path>`; an embedded text resource is
   inlined as `--- <uri> ---\n<text>`; a resource link adds `Attached: <uri>`. Text-only
   prompts are unchanged. Slash-command detection still uses the text only.
5. `ide_pages/orch.py` "Thread defaults": show Model, plus Recipe and Workspace as
   "defaults the orchestrator proposes" (no Mode).

## Tests

- new sessions offer exactly one config option (model) and no modes; loaded sessions
  too; `set_config_option("recipe", …)` still accepted.
- `initialize` → `prompt_capabilities.image is True`, `embedded_context is True`.
- a prompt with a PNG image block writes the file under
  `<home>/attachments/<session>/`, and the orchestrator receives the text plus
  `Attached image: <path>`; an embedded text resource is inlined.
- harness argv: `--allowedTools` includes Edit, Write, MultiEdit, Bash;
  `--disallowedTools` keeps NotebookEdit.
- `/run <task>` still starts a direct run.

## Done when

- `/tmp/chunked-gate.sh tests/unit/test_acp_agent_py.py tests/unit/test_ide_pages_orch.py <harness tests>`
  passes (chunks: a long pytest process is killed by the machine's CPU guard); paste
  its last line.
- ruff clean on touched files; diff touches only files in scope.
