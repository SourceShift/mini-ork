# Zed S3a — browse and inspect recipes: `/recipes` and `/recipe <name>`

Depends on S0 (`mini_ork/recipes_catalog.py`) and S2 merged. Serial with the other Zed slices.

## Goal

A user deciding how to get work done needs to see the recipes: what each is for, which
requests it handles, its steps and which model runs each, how it checks its work, how good
it is, and how it has done before. Today there is no way to browse or inspect a recipe in
Zed. Add a recipe table and a recipe card, and give the orchestrator the same data.

## Files in scope

- `mini_ork/acp/recipe_view.py` — NEW: card data + markdown.
- `mini_ork/acp/commands.py` — `/recipes`, `/recipe`; replies may carry file links.
- `mini_ork/acp/agent.py` — emit file links after a command's text.
- `mini_ork/mcp_context/server.py` — `describe_recipe` tool.
- `tests/unit/test_acp_recipe_view.py` — NEW.
- `tests/unit/test_acp_commands.py`, `tests/unit/test_mcp_context_server.py`,
  `tests/unit/test_acp_agent_py.py` — extend.

No other file changes.

## Mechanism (exact spec)

### `recipe_view.py`

```python
def recipe_rows(home: Path | None, *, source: str = "all", text: str = "") -> list[dict]
def render_recipes(rows: list[dict], *, source: str) -> str
def recipe_card(home: Path | None, recipe_id: str) -> dict | None
def render_recipe_card(card: dict) -> tuple[str, list[Path]]     # markdown, files to link
```

Data sources (read the files before coding — shapes below are from real recipes):
- Catalog: `mini_ork.recipes_catalog.list_recipes(home)` / `find_recipe` (id, source, path,
  description, task_class, node_count, shadows_engine).
- `task_class.yaml`: `name`, `description`, `matches.keywords` (list of phrases).
- `workflow.yaml`: `description`, `nodes` (each: `name`, `type`, `model_lane`, `prompt_ref`,
  `verifier_ref`, `gates`), `edges` (`from`, `to`, `edge_type`).
- `artifact_contract.yaml`: top-level keys such as `expected_artifact`,
  `success_verifiers`, `failure_policy`, `rollback_policy` (render what is present).
- Model per lane: `mini_ork.web.recipes.load_lanes(home)` maps a node's `model_lane` (a role)
  to a lane id; show `role → lane`.
- Grade: `mini_ork.cli.recipe_eval.eval_recipe(<recipe's parent of recipes/>, id)` → `score`,
  `findings` (`sev`, `msg`, `fix`); grade letter by the module's `_grade(score)`. For a
  project recipe the root is the home; for an engine recipe the engine root.
- Track record: one query over `task_runs` grouped by `recipe` in the home's db
  (`mini_ork.web.deps.db_for(home)`): runs, published count, average `cost_usd`, average
  duration (`updated_at - created_at`) of finished runs; the card also lists the last 5 runs
  (id, `task_state.run_mark`, age, cost).
- Example kickoff: `<recipe>/example-kickoff.md` or the first `<recipe>/examples/*/kickoff.md`.

`recipe_rows`: one dict per catalog entry with id, source label (`project`,
`project (overrides engine)`, `engine`), steps (node_count), grade letter + score, runs,
success % (published / finished), avg cost; filter `source` (`all|project|engine`) and a
case-insensitive `text` match on id/description/keywords. Sorted: project first, then by
runs desc, then id.

`render_recipes`: a line with counts (`Project 2 · Engine 34`), then
`| recipe | source | steps | grade | runs | success | avg $ | what it does |` (description
≤ 70 chars), then "Details: `/recipe <name>` · Filter: `/recipes project`,
`/recipes <text>`". Empty → "No recipes match."

`recipe_card`: None when unknown; else everything above plus the files to link
(`workflow.yaml`, `task_class.yaml`, `artifact_contract.yaml`, every `prompts/*.md`, every
`verifiers/*`, the example kickoff).

`render_recipe_card`: title `**<id>** — <source>`; full description; "Chosen for requests
like:" with ≤ 8 keywords; a steps table `| step | type | model (role → lane) | checks |`
in workflow order, where checks lists the node's `verifier_ref` file name and `gates`;
a "Flow:" line from the edges (`planner → doc_editor → grep_assert, link_verifier →
publisher`, branches joined by commas); what it produces / how success is verified (from
the artifact contract); grade line (`A — 95/100`) and each finding with its fix; track
record (`41 runs · 93% success · avg $0.45 · avg 2m10s`) and the last runs table; the
example kickoff's first heading; then "Run it: `/run <task>` with Recipe = <id>, or ask
the orchestrator." Returns the markdown and the list of file paths.

### Commands and links

- `/recipes [project|engine] [text]` → `render_recipes`.
- `/recipe <id>` → the card text, plus file links; unknown id → "No recipe <id>. `/recipes`
  lists them."; `/recipe` alone → usage.
- Handlers may return either a string (as today) or a `CommandReply(text: str, links:
  list[Path])` (dataclass in `commands.py`). In `agent.py`, after emitting the text as an
  `AgentMessageChunk`, emit one `AgentMessageChunk` per link whose content is
  `ResourceLink(type="resource_link", uri=path.resolve().as_uri(), name=<path relative to
  the recipe dir>)` — Zed renders these as clickable file mentions.
- Announce `recipes` and `recipe` in the commands list.

### MCP

`describe_recipe(id)` (read-only, also without `--control`): the card dict with paths as
strings; unknown → error object. Description: "Everything about one recipe: purpose,
request keywords, steps with their model roles, checks, grade with fix hints, track record,
files." Extend `list_recipes` entries with `grade` and `runs` (cheap: score from
`eval_recipe`, runs from the grouped query).

## Tests

`tests/unit/test_acp_recipe_view.py` (temp engine root + temp home with a project recipe
shaped like `recipes/docs`, a migrated db copied once per module with seeded task_runs):
rows for both sources incl. an override; filters; ordering; grade from a recipe missing
`artifact_contract.yaml`; track record numbers; card steps/flow/checks/contract/keywords;
links list; unknown id.

`tests/unit/test_acp_commands.py`: `/recipes`, `/recipes project`, `/recipe docs`, unknown.
`tests/unit/test_acp_agent_py.py`: a `/recipe` reply emits the text then ResourceLink chunks.
`tests/unit/test_mcp_context_server.py`: `describe_recipe` hit/miss; `list_recipes` has
grade and runs.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_acp_recipe_view.py tests/unit/test_acp_commands.py tests/unit/test_acp_agent_py.py tests/unit/test_mcp_context_server.py tests/unit/test_recipes_catalog.py` passes.
- `ruff check` clean; diff touches only the files in scope.
