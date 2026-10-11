# Zed S7b — kickoff helper: draft the kickoff with the user, check it, then start the run

Depends on S7a merged. Serial with the other slices that edit `mini_ork/acp/agent.py`.

## Goal

A run is only as good as its kickoff. Today the orchestrator writes a kickoff and starts
the run in one step, and in a Zed run nobody is there to answer the planner's profile
questions, so a model answers them itself. `/kickoff <task>` (or "write me a kickoff
for …, I want to check it first") makes the kickoff a reviewed artifact: the orchestrator
reads the recipe and the code, drafts the kickoff with real paths and a real success
check, mini-ork checks it deterministically, and the thread shows it as a new file plus
the findings. The user decides with buttons — **Start run** / **Save only** / **Change
something** / **Discard**.

## Files in scope

- `mini_ork/kickoff_lint.py` — NEW: deterministic kickoff checks.
- `mini_ork/mcp_context/server.py` — control tool `draft_kickoff`.
- `mini_ork/acp/agent.py` — draft display, buttons, start / save.
- `mini_ork/acp/commands.py` — `/kickoff`; help text.
- `mini_ork/acp_orchestrator/prompt.md` — a "Writing a kickoff the user checks first" section.
- `tests/unit/test_kickoff_lint.py` — NEW; `tests/unit/test_mcp_context_server.py`,
  `tests/unit/test_acp_agent_py.py`, `tests/unit/test_acp_commands.py` — extend.

No other file changes.

## Mechanism (exact spec)

### `mini_ork/kickoff_lint.py`

```python
def lint(markdown: str, *, project: Path, recipe: str | None = None, home: Path | None = None) -> list[dict]
def slug(markdown: str) -> str
```

`lint` returns `[{"sev": "error"|"warn", "msg", "fix"}]`, never raises:
- error: empty or whitespace-only.
- warn: no `# ` title line.
- warn: no "Files in scope" section, when the recipe has an `implementer` node (read the
  recipe's workflow, home-first via `mini_ork.planning.recipe_plan.recipe_dir`). Parse the
  section with `mini_ork.orchestration.concord_admission.parse_scope`.
- warn per scope path that does not exist under `project` (globs: no match) unless its
  bullet/line says `(new)` (case-insensitive): msg `"Not in the project: <path>"`, fix
  `"Fix the path, or mark it (new) if the run creates it."`.
- warn: no success section — a heading containing "success", "acceptance", "done when" or
  "verification" (case-insensitive): fix `"Say how success is checked — ideally a command
  that exits 0."`.
- warn per `## ` heading present in the recipe's example kickoff (`examples/*/kickoff.md`,
  else `example-kickoff.md`) but absent here: `"The <recipe> examples have a '## <h>'
  section."`.
- warn: longer than 20 000 characters.

`slug`: from the title (lowercase, `[a-z0-9-]`, ≤ 48 chars, collapse dashes), else
`kickoff`.

### MCP (`--control`)

`draft_kickoff` (args `recipe`, `kickoff_markdown`) → recipe must exist
(`recipes_catalog.find_recipe`) else `{"ok": false, "error"}`; write
`<home>/kickoff-drafts/<slug>.md` (atomic; replaces a draft of the same slug) and return
`{"ok": true, "draft_id": <slug>, "recipe", "path", "findings": lint(...)}` with `project =
home.parent`. Description: "Show the user a kickoff before any run starts. It does NOT
start a run: the user starts it with a button. Fix every finding of severity error and
draft again." Never starts a run.

### Agent

- `_prompt_thread_orchestrate`: track `draft_kickoff` tool_use ids like `draft_recipe`.
  On its `ok: true` result: remember it as this turn's pending kickoff (`draft_id`,
  `recipe`, tool call id) and emit a `ToolCallProgress` on that tool call with a
  `FileEditToolCallContent(type="diff", path=<home>/kickoffs/<draft_id>.md, old_text=None,
  new_text=<markdown>)` and a text block: `"Looks complete."` when there are no findings,
  else one line per finding (`⚠ msg — fix`, errors as `✗ msg — fix`).
- After the turn (after any recipe draft and automation proposal offers), when a pending
  kickoff exists: `request_permission` on its tool call, title `Start <recipe> run?`,
  options `run` ("Start run", allow_once), `save` ("Save only", allow_always), `change`
  ("Change something", reject_once), `discard` ("Discard", reject_always).
  - `run` → delete the draft and start the run exactly as `/run` does with that recipe and
    the kickoff text (worktree per the thread's Workspace option, followed in the thread;
    the turn ends when the run ends).
  - `save` → move the draft to `<home>/kickoffs/<draft_id>.md` (an existing file → `-2`,
    `-3` …); message ``"Saved `.mini-ork/kickoffs/<name>.md`. Run it later with
    `/run` or `mini-ork run <recipe> .mini-ork/kickoffs/<name>.md`."`` + a file link.
  - `change` → `"Tell me what to change."` (draft kept); `discard` → delete,
    `"Kickoff discarded."`; dismissed → `"Kickoff kept — ask me to start it when you're
    ready."`.
  - Mark the tool call `completed` for run/save, `failed` otherwise.

### `/kickoff [task]`

Thread only (run session → `"Kickoffs are written in a mini-ork thread — start one from
New Thread."`). Returns the rewrite sentinel with text `"The user wants a kickoff for the
<recipe> recipe (the thread's Recipe picker; suggest another if it fits better): <task, or
'Ask what the run should do.'> Follow your kickoff steps."` and bridge `"Handing this to
the orchestrator. When the kickoff is ready you'll see it here with the buttons to start
the run."`. Announce `kickoff` ("Draft a kickoff with the orchestrator, check it, then
start the run.", hint "what the run should do").

### Orchestrator prompt — "Writing a kickoff the user checks first"

When the user asks for a kickoff to review (or `/kickoff`): call `describe_recipe` for
what the recipe expects and its example; read the code (Read/Grep/Glob) so `## Files in
scope` names real paths (mark files the run creates `(new)`); write a title, what to do
and why, `## Files in scope`, `## Success criteria` (commands that exit 0), and what is
out of scope; call `draft_kickoff`; if it returns error findings, fix and draft again;
summarize in one or two lines. Never call `start_run` for a kickoff the user wants to
check — the user starts it with the button.

## Tests

`tests/unit/test_kickoff_lint.py`: each finding (empty; no title; no scope for an
implementer recipe vs a researcher-only recipe; missing path; `(new)` path accepted; glob
match; no success section; example headings; long), a complete kickoff → `[]`; `slug`.

`tests/unit/test_mcp_context_server.py`: `draft_kickoff` only with control; writes the draft
and returns findings; unknown recipe → error; never launches (launch_run monkeypatched to
fail the test if called).

`tests/unit/test_acp_agent_py.py` (`asyncio.run`, autouse guard, fake orchestrator turn
with a `draft_kickoff` tool_use + result from the real MCP helper in a temp git project,
`_PermConn`): the draft shows as a new-file diff + findings; `run` launches the recipe with
the kickoff via the launcher seam and follows it; `save` writes `.mini-ork/kickoffs/<slug>.md`
(and `-2` on a clash) with a link; `change` keeps it; `discard` deletes it; dismissed keeps
it; `ok: false` asks nothing; `/kickoff fix the login redirect` reaches the orchestrator
with the rewritten text and the bridge.

`tests/unit/test_acp_commands.py`: `/kickoff` in a thread returns the sentinel; in a run
session the message; announced.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_agent_py.py tests/unit/test_acp_commands.py tests/unit/test_kickoff_lint.py tests/unit/test_mcp_context_server.py` passes.
- `ruff check` clean; diff touches only the files in scope.
