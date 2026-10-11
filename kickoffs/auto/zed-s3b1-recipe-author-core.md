# Zed S3b-1 — recipe authoring core: spec → recipe files → validation → draft → commit

Independent of the ACP agent (the guided Zed flow is S3b-2). Creates project recipes in
`<home>/recipes/<id>/` — the location runs already resolve first.

## Goal

The orchestrator will guide a user through creating a recipe: it interviews them, fills in
a small structured spec, and mini-ork turns that spec into a correct recipe deterministically
— files rendered from the spec, validated against the schemas and the recipe evaluator,
staged as a draft the user approves, then committed into the project. A model never writes
the YAML directly. This slice builds that core and exposes it as MCP tools.

## Files in scope

- `mini_ork/recipe_author.py` — NEW.
- `mini_ork/mcp_context/server.py` — tools `recipe_guide`, `draft_recipe`, `get_recipe_spec` (control mode).
- `tests/unit/test_recipe_author.py` — NEW.
- `tests/unit/test_mcp_context_server.py` — extend.

No other file changes.

## Mechanism (exact spec)

### The spec (JSON)

```json
{
  "id": "sql-migration-audit",
  "description": "Audit a SQL migration for locking and rollback risks.",
  "keywords": ["audit migration", "migration review", "sql migration"],
  "input": "The path of the migration file to audit.",
  "steps": [
    {"id": "analyzer", "type": "researcher", "role": "planner",
     "instructions": "Read the migration named in the kickoff and list ..."},
    {"id": "schema_check", "type": "verifier", "check": "alembic check", "after": ["analyzer"]},
    {"id": "reviewer", "type": "reviewer", "role": "reviewer",
     "instructions": "...", "after": ["analyzer", "schema_check"]}
  ],
  "publish": false,
  "rollback_on_failure": false,
  "example_kickoff": "# Audit migration 0042\n\n## Files in scope\n\n- db/migrations/0042_x.sql\n"
}
```

- `id`: `^[a-z][a-z0-9-]{2,47}$`. `description`: 1–400 chars. `keywords`: 1–12 phrases.
- `steps`: 1–12; each `id` `^[a-z][a-z0-9_]{1,31}$`, unique; `type` one of `planner`,
  `researcher`, `implementer`, `reviewer`, `verifier`, `eval`; model steps (all but
  `verifier`) need `role` (a key of the home's lane map, `mini_ork.web.recipes.load_lanes`)
  and `instructions` (markdown, ≤ 20 000 chars); `verifier` steps need `check` (a shell
  command; exit 0 = pass) and no role; `after`: ids of earlier steps.
- `publish` (default `true`), `rollback_on_failure` (default `false`), `example_kickoff`
  (optional markdown).

### `recipe_author.py`

```python
SPEC_SCHEMA: dict                                    # JSON Schema of the spec above
def guide(home: Path | None) -> dict
def validate_spec(spec: dict, home: Path | None) -> list[dict]      # [{"sev": "error"|"warn", "msg", "fix"}]
def render(spec: dict) -> dict[str, str]                             # relative path -> file content
def draft(home: Path, spec: dict, *, base: str | None = None) -> dict
def commit_draft(home: Path, recipe_id: str) -> dict
def discard_draft(home: Path, recipe_id: str) -> dict
def get_spec(home: Path | None, recipe_id: str) -> dict | None
```

- `guide`: `{"spec_schema": SPEC_SCHEMA, "step_types": {type: one-line meaning},
  "roles": {role: lane} from the home's lane map, "rules": [short authoring rules: keep it
  to the fewest steps that do the job; every recipe needs at least one verifier or reviewer
  step; checks must be commands that exit 0 on success; instructions say what to read, what
  to produce, and what not to touch], "example": <the spec above>}`.
- `validate_spec`: schema errors (jsonschema, `pyproject` already depends on it) → errors with
  the JSON path; unknown `after` ids, cycles, a step depending on itself → errors; unknown
  `role` → error listing valid roles; no `verifier`/`reviewer`/`eval` step → warning; an `id`
  that collides with an ENGINE recipe → warning ("your project recipe will override the
  engine's <id>"); never raises.
- `render` (deterministic; same spec → same bytes; YAML via `yaml.safe_dump(sort_keys=False)`):
  - `task_class.yaml`: `name` = id with `-`→`_`, `task_class` = same, `version: "0.1.0"`,
    `description`, `artifact_contract_ref: artifact_contract.yaml`,
    `default_workflow: workflow.yaml`, `matches: {keywords: [...]}`, `default_gates: []`,
    `risk_class: low`.
  - `workflow.yaml`: `version: "0.1.0"`, `task_class`, `description`, `nodes` — one per step
    (`name` = step id, `type`, `dispatch_mode: serial`, `model_lane` = role for model steps,
    `prompt_ref: prompts/<id>.md` for model steps, `verifier_ref: verifiers/<id>.py` for
    verifier steps), then `publisher` (when `publish`) and `rollback` (when
    `rollback_on_failure`); `edges` — for each `after` id a `{from: after, to: step,
    edge_type: depends_on}` (`verifies` when the step is a verifier and the source is an
    implementer); steps with no `after` and not first depend on the previous step;
    `publisher` depends on every step nothing else depends on; `rollback` gets
    `{from: publisher, to: rollback, edge_type: escalates_to}` (or from the last step when
    there is no publisher).
  - `artifact_contract.yaml`: `task_class`, `expected_artifact: <id>_result`,
    `success_verifiers: [verifiers/<id>.py ...]`, `failure_policy: request_changes`,
    `rollback_policy` text (one line, by `rollback_on_failure`).
  - `prompts/<id>.md`: the step's instructions, preceded by a line `<!-- step <id> of recipe <recipe id> -->`.
  - `verifiers/<id>.py`: a Python script that runs the `check` with `subprocess.run(check,
    shell=True, capture_output=True, text=True, timeout=900)` in the current directory and
    prints ONE JSON line `{"pass": rc == 0, "check": <command>, "rc": rc, "output_tail":
    <last 2000 chars of stdout+stderr>}`; timeout → `pass: false`. (The verifier runner
    treats `{"pass": true}` as success and empty output as a failure.)
  - `example-kickoff.md` (from `example_kickoff`, else a template with the input and a
    "## Files in scope" section), `README.md` (purpose, input, steps table, how to run:
    `mini-ork run <id> <kickoff.md>`), `recipe.spec.json` (the spec, pretty-printed — makes
    later edits exact).
- `draft`: `validate_spec`; on errors return `{"ok": false, "errors": [...]}` and write
  nothing. Otherwise write the rendered files to `<home>/recipe-drafts/<id>/` (replacing an
  older draft of the same id) plus `draft.json` (`spec`, `base`, `created_at`), run
  `mini_ork.cli.recipe_eval.eval_recipe(<home>/recipe-drafts, <id>)` on it, and return
  `{"ok": true, "draft_id": id, "target": "<home>/recipes/<id>", "exists": <target exists>,
  "files": [{"path": rel, "content": text, "previous": <current text at target or null>}],
  "warnings": [...], "grade": {"score", "letter", "findings"}}`. `base` (editing an existing
  project recipe) must equal `id` when given.
- `commit_draft`: requires the draft; when the target exists, move it to
  `<home>/recipe-backups/<id>-<UTC timestamp>/` first; copy the draft files (not
  `draft.json`) into a temp dir beside the target and rename it into place; delete the
  draft; return `{"ok": true, "path": target, "backup": path|null}`. Errors → `{"ok":
  false, "error"}`.
- `discard_draft`: delete the draft dir; `{"ok": true}` (also when absent).
- `get_spec`: the parsed `recipe.spec.json` of a project or engine recipe (via
  `recipes_catalog.find_recipe`), else None.

### MCP (`--control` only)

- `recipe_guide` → `guide(home)`.
- `draft_recipe` (`spec` object, optional `base`) → `draft(home, spec, base=...)`. It never
  writes into `recipes/`; committing is the user's approval step (S3b-2).
- `get_recipe_spec` (`id`) → the spec or an error saying the recipe was not authored from a
  spec (edit it by copying it into the project).
Tool descriptions say exactly that. Do NOT add a commit tool to MCP.

## Tests

`tests/unit/test_recipe_author.py` (temp home with `config/agents.yaml` lane map; the engine
root monkeypatched to a temp dir with one recipe): guide content; each validation error and
warning; render is deterministic and the rendered workflow/task_class/artifact_contract pass
`schemas/*.schema.json` (load the repo's schema files); edges for after/verifies/default
chaining/publisher/rollback; the generated verifier passes for `true` and fails for `false`
(run it as a subprocess and parse its JSON); draft writes only under recipe-drafts and
reports grade + previous contents; commit moves into recipes/ with a backup when replacing;
discard; get_spec round trip; the committed recipe is found by
`mini_ork.recipes_catalog.find_recipe` as a project recipe.

`tests/unit/test_mcp_context_server.py`: the three tools listed only with control; a
`draft_recipe` call writes a draft and returns ok; an invalid spec returns errors; no
commit tool exists.

## Success criteria

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_recipe_author.py tests/unit/test_mcp_context_server.py tests/unit/test_recipes_catalog.py` passes.
- `ruff check` clean; diff touches only the files in scope.
