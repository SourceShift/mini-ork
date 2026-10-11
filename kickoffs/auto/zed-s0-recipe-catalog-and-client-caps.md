# Zed S0 — one recipe catalog (project + engine), and record what the editor supports

## Goal

Two foundations for the next Zed slices.

1. **Project recipes are invisible.** A run already resolves a recipe from the project's
   `.mini-ork/recipes/<name>/` before the engine's `recipes/<name>/`
   (`mini_ork/cli/main.py:_resolve_recipe_base` — home first, then cwd, then root). But the
   Zed recipe picker (`MiniOrkAcpAgent._list_recipes`, via `mini_ork.web.recipes.list_recipes`)
   and the MCP `list_recipes` tool (`mini_ork/mcp_context/server.py:_list_recipes`) only scan
   the engine. A recipe a user creates for their project can be run but never seen. Add one
   catalog both use.
2. **We do not know which ACP client features Zed offers** (form elicitation, file writes,
   terminals). Keep the capabilities the client sends in `initialize` and log them once, so
   later slices can use forms and file writes where offered and fall back otherwise.

## Files in scope

- `mini_ork/recipes_catalog.py` — NEW.
- `mini_ork/acp/agent.py` — picker uses the catalog; keep client capabilities.
- `mini_ork/mcp_context/server.py` — `list_recipes` uses the catalog.
- `tests/unit/test_recipes_catalog.py` — NEW.
- `tests/unit/test_acp_agent_py.py`, `tests/unit/test_mcp_context_server.py` — extend.

No other file changes.

## Mechanism (exact spec)

### `recipes_catalog.py`

```python
@dataclass(frozen=True)
class RecipeInfo:
    id: str                 # directory name
    source: str             # "project" | "engine"
    path: Path              # the recipe directory
    description: str        # first line of task_class.yaml `description` (≤ 120 chars), else workflow.yaml `description`, else ""
    task_class: str         # task_class.yaml `name`, else ""
    node_count: int         # len(workflow.yaml `nodes`) (0 when unreadable)
    shadows_engine: bool    # a project recipe with the same id as an engine recipe

def list_recipes(home: Path | None) -> list[RecipeInfo]
def find_recipe(recipe_id: str, home: Path | None) -> RecipeInfo | None
```

- A recipe is a directory containing both `workflow.yaml` and `task_class.yaml`.
- Sources: `<home>/recipes/*` (project; skipped when `home` is None or has no `recipes/`)
  then `<engine root>/recipes/*` (engine root = `mini_ork.web.control._mini_ork_root()`,
  what `launch_run` uses). A project recipe wins over an engine recipe with the same id
  (mirrors `_resolve_recipe_base`); the engine one is then omitted and the project one has
  `shadows_engine=True`. If `<home>/recipes` resolves to the same real path as the engine's
  `recipes/` (a dev checkout where home is the engine), list each recipe once as `engine`.
- YAML read with `yaml.safe_load`; a recipe whose YAML does not parse is still listed with
  empty description/task_class and `node_count=0` (never raise). Sorted by id.
- `find_recipe` returns the entry with that id from the same resolution, else None.

### `agent.py`

- `_list_recipes()` → `_list_recipes(home)` returning `[r.id for r in recipes_catalog.list_recipes(home)]`
  (same fallbacks as today on any error). The recipe config option uses the session's home
  (`self._home_for(session_id)`); option descriptions: `"project recipe"` /
  `"project recipe (overrides the engine's)"` / the recipe description for engine ones.
- `initialize(..., client_capabilities=...)`: store the capabilities on the agent
  (`self._client_capabilities`, the SDK object or None) and write ONE stderr line,
  `[mini-ork acp] client capabilities: fs.read=<bool> fs.write=<bool> terminal=<bool> elicitation=<summary>`,
  where `<summary>` lists the advertised elicitation modes (e.g. `form,url`) or `none`
  (introspect `acp.schema.ClientCapabilities` for the field names; unknown/missing → `none`).
  Add `def client_supports(self, feature: str) -> bool` for `"fs.write"`, `"fs.read"`,
  `"terminal"`, `"elicitation.form"` (False when unknown).

### `mcp_context/server.py`

- `list_recipes` uses `recipes_catalog.list_recipes(home)`; each entry
  `{"id", "description", "source", "nodes": node_count}` plus `"overrides_engine": true` when
  set. Update the tool description to say project recipes are included and win on a name clash.

## Tests

`tests/unit/test_recipes_catalog.py` (temp engine root via monkeypatching the root resolver,
temp home): engine-only; project-only; project shadows engine (one entry, source project,
`shadows_engine`); home == engine recipes dir (no duplicates); directory missing
`task_class.yaml` skipped; broken YAML listed with defaults; description truncation;
`find_recipe` hit and miss; `home=None`.

`tests/unit/test_acp_agent_py.py` (no pytest-asyncio — `asyncio.run`; keep the autouse
guard): the recipe picker of a thread whose project has `.mini-ork/recipes/my-audit/`
lists `my-audit`; `initialize` with a capabilities object stores it and `client_supports`
answers per feature; with none, every feature is False and nothing raises.

`tests/unit/test_mcp_context_server.py`: `list_recipes` includes a project recipe with
`source: "project"`.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_recipes_catalog.py tests/unit/test_acp_agent_py.py tests/unit/test_mcp_context_server.py tests/unit/test_acp_commands.py` passes.
- `ruff check` clean; diff touches only the files in scope.
