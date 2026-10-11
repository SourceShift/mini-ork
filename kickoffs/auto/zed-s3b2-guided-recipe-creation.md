# Zed S3b-2 — create and edit recipes in a Zed thread: interview → draft diffs → buttons

Depends on S3a and S3b-1 merged (`mini_ork/recipe_author.py`, MCP `recipe_guide` /
`draft_recipe` / `get_recipe_spec`, `/recipe <id>` card with file links). Serial with the
other Zed slices.

## Goal

A user says "I want a recipe that audits our SQL migrations" (or types `/recipe new …`).
The orchestrator interviews them (what a run receives, the steps, how success is checked,
which models), drafts the recipe through `draft_recipe`, and the thread shows every file of
the draft as a diff plus its grade. The user decides with buttons — **Create recipe** /
**Change something** / **Discard draft** — and on Create the recipe lands in
`.mini-ork/recipes/<id>/`, appears in the Recipe picker, and the user can test it right away.
Editing works the same way (`/recipe edit <id>`).

## Files in scope

- `mini_ork/acp/agent.py` — draft display, approval buttons, commit/discard, test-run offer,
  `/recipe new|edit` routing.
- `mini_ork/acp/commands.py` — `/recipe new` and `/recipe edit` hand off to the orchestrator;
  help text.
- `mini_ork/acp_orchestrator/prompt.md` — a "Creating or changing a recipe" section.
- `mini_ork/recipe_author.py` — add `copy_to_project(home, recipe_id) -> dict`.
- `tests/unit/test_acp_agent_py.py`, `tests/unit/test_acp_commands.py`,
  `tests/unit/test_recipe_author.py` — extend.

No other file changes.

## Mechanism (exact spec)

### Orchestrator prompt (append a section)

"Creating or changing a recipe": call `recipe_guide` first; ask at most 2–3 short questions
per message, in this order of need — what a run receives (input), the steps (propose the
fewest that do the job: e.g. one implementer + one check), how success is checked (a shell
command that exits 0 — ask for it), which model roles (offer the roles from the guide,
default `worker` for doing and `reviewer` for reviewing), whether to publish; then call
`draft_recipe` with the spec and summarize the grade and any warnings in one or two lines.
Never say a recipe is created — the user creates it with the buttons under the draft. To
change an existing recipe call `get_recipe_spec` first and draft with `base` = its id;
if it has no spec, say the user can copy it into the project (`/recipe edit <id>` offers
that). Keep ids short and lowercase with hyphens.

### Draft display (orchestrate turn)

- In `_prompt_thread_orchestrate`'s event handler, track `draft_recipe` tool_use ids (the
  name after the MCP prefix, as done for `start_run`). On the matching tool_result whose
  JSON has `ok: true`: remember it as this thread's pending draft (`draft_id`, the tool call
  id, `exists`, grade, warnings) and emit a `ToolCallProgress` for that tool call id with
  content: one `FileEditToolCallContent(type="diff", path=<abs target path:
  <home>/recipes/<id>/<rel>>, old_text=<previous or None>, new_text=<content>)` per file
  except `recipe.spec.json`, then a `ContentToolCallContent` text block
  `Grade <letter> (<score>/100)` + one line per warning/finding. `ok: false` → nothing
  extra (the orchestrator explains the errors).

### Approval buttons (after the turn)

- After the orchestrator turn returns successfully, if a pending draft was produced in this
  turn, call `await self._conn.request_permission(session_id=<thread>,
  tool_call=ToolCallUpdate(tool_call_id=<draft tool id>, title=("Update" if exists else
  "Create") + f" recipe {id}", kind="edit", status="pending"), options=[
  PermissionOption(option_id="create", name="Update recipe" if exists else "Create recipe",
  kind="allow_once"), PermissionOption(option_id="change", name="Change something",
  kind="reject_once"), PermissionOption(option_id="discard", name="Discard draft",
  kind="reject_always")])` (read `acp.schema` for the exact types; the response's
  `outcome` is `AllowedOutcome(option_id=…)` or `DeniedOutcome` = dismissed).
  - `create` → `recipe_author.commit_draft(home, id)`; on success emit a message
    "Created `.mini-ork/recipes/<id>` — it is in the Recipe picker now." (or "Updated …";
    mention the backup path when one was made), then file links (ResourceLink chunks, the
    same helper `/recipe <id>` uses), then offer a test with a second `request_permission`:
    options `test` ("Test it now") / `later` ("Not now"). `test` → launch a run of that
    recipe with its example kickoff text (`examples/*/kickoff.md`) through the existing
    launcher seam and follow it in the thread exactly as a direct-mode run (marker, live
    output, plan, diffs, title); the turn ends when that run ends. Commit failure → message
    with the error, draft kept.
  - `change` → message "Tell me what to change." (draft kept).
  - `discard` → `discard_draft`; message "Draft discarded."
  - dismissed (`DeniedOutcome`) → keep the draft; message "Draft kept — ask me to create
    it when you're ready."
  - Every message is recorded in the thread (as all thread output is).
- A pending draft is cleared after create/discard; a later turn that drafts again replaces it.

### `/recipe new` and `/recipe edit` (threads)

- `/recipe new [what it should do]` → not answered by a command handler: the agent turns it
  into an orchestrator prompt "The user wants to create a new recipe. <what it should do, or
  'Ask what it should do.'> Follow your recipe-creation steps." and runs the orchestrate
  turn with it (mode is irrelevant: this always goes to the orchestrator). In a run session
  → "Recipes are created in a mini-ork thread — start one from New Thread."
- `/recipe edit <id>`:
  - project recipe with `recipe.spec.json` → orchestrator prompt "The user wants to change
    recipe <id>. Ask what to change, then draft it with base=<id>."
  - otherwise (engine recipe, or no spec) → `request_permission` with options `copy`
    ("Copy into this project") / `cancel` ("Cancel"), title "Copy recipe <id> into the
    project to edit it". `copy` → `recipe_author.copy_to_project(home, id)` → message
    "Copied to `.mini-ork/recipes/<id>` — edit its files below; the project copy overrides
    the engine's." + file links. Unknown id → "No recipe <id>. `/recipes` lists them."
- Mechanism: the slash dispatch returns a sentinel carrying the rewritten prompt text, and
  `prompt()` runs the orchestrate path with that text (record the user's original command
  as the user record, as commands are recorded today).

### `recipe_author.copy_to_project(home, recipe_id)`

Copy an engine recipe's directory into `<home>/recipes/<id>/` (error when a project recipe
with that id already exists or the id is unknown); returns `{"ok", "path", "files": [rel
paths]}`.

## Tests

`tests/unit/test_acp_agent_py.py` (no pytest-asyncio — `asyncio.run`; keep the autouse
guard; a fake orchestrator turn emitting a `draft_recipe` tool_use + tool_result built from
a real `recipe_author.draft` in a temp home; a fake connection whose `request_permission`
returns scripted outcomes and records calls): the draft shows as diffs (new files have
`old_text` None; an edit shows previous text) plus the grade line; `create` commits (files
exist under `.mini-ork/recipes/<id>`, message + links, test offer made); `test` launches the
recipe with its example kickoff via the launcher seam; `change` keeps the draft; `discard`
deletes it; dismissed keeps it; a failed draft (`ok: false`) asks nothing; `/recipe new
audit migrations` reaches the orchestrator turn with the rewritten text; `/recipe edit
<engine id>` offers copy and `copy` creates the project copy; `/recipe edit` of a spec'd
project recipe reaches the orchestrator.

`tests/unit/test_recipe_author.py`: `copy_to_project` happy path, existing project recipe,
unknown id.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_agent_py.py tests/unit/test_acp_commands.py tests/unit/test_recipe_author.py tests/unit/test_acp_recipe_view.py tests/unit/test_mcp_context_server.py` passes.
- `ruff check` clean; diff touches only the files in scope.
