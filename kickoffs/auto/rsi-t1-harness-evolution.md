# RSI Tier-1 #7a: typed, schema-checked, reversible workflow.yaml edits

## Goal

The apply loop can only mutate prompt files today. `harness.<recipe>.<node>`
targets (`mini_ork/cli/apply.py:_harness_target_file_for`, c7bcace8, opt-in
`MO_APPLY_HARNESS_TARGETS=1`) resolve to the node's `prompt_ref` file and
append a directive block to it. Node order, lane pins, tool grants and retry
caps in `recipes/<recipe>/workflow.yaml` are not mutable by the loop at all.
`mini_ork/learning/harness_operator.py:materialize_mutation` raises
`ValueError` for every non-prompt surface (`stage_order`, `recovery`,
`routing`, `verifier`) for exactly this reason.

This kickoff adds the first slice: a **pure library** that turns ONE typed
edit into a validated, compiled, reversible `workflow.yaml` text, without
writing the live file. Nothing calls it yet. The follow-on kickoff
`rsi-t1-harness-evolution-apply` wires it into the apply gate (probe scorer,
control arm, quarantine). That keeps this change independently mergeable with
zero behaviour change.

Runtime facts this spec relies on (verified on origin/main 8ae661f3):

- Execution order is `mini_ork/workflow/compiler.py:_stable_topological_order`.
  The DAG comes from the edges, and the declared `nodes` list order breaks
  ties between ready nodes. So moving a node in the list can change execution
  order, or do nothing.
- The live per-node retry knob is the `retry:` block's `max_attempts`
  (`mini_ork/workflow/retry.py`, runtime default 3). The node-level integer
  `retries` field is not read by the executor, so this library does not edit it.
- Tool grants are read only in dict form, `tools: {native: [...], mcp: [...]}`
  (`mini_ork/dispatch/providers.py`, `mini_ork/dispatch/mcp_registry.py:node_mcp_grants`).
- 48 of 50 recipes pass `schemas/workflow.schema.json`, and all 50 compile
  after a `yaml.safe_dump` round trip.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. `mini_ork/learning/workflow_edit.py` (new)

Imports: `copy`, `hashlib`, `json`, `re`, `tempfile`, `pathlib.Path`, `yaml`,
`jsonschema.validators.validator_for`, and from `mini_ork.workflow.compiler`
both `compile_workflow` and `WorkflowCompileError`. No DB, no network, no
environment reads.

**Module constants (exact names and values):**

- `OPS: tuple[str, ...] = ("node_order", "lane_pin", "tool_grant", "retry_count")`
- `TOOL_GROUPS: tuple[str, ...] = ("native", "mcp")`
- `LANE_NODE_TYPES: frozenset[str] = frozenset({"planner", "researcher", "implementer", "reviewer", "eval", "reflector"})`
- `RETRY_MIN = 1`
- `RETRY_MAX = 5`
- `DEFAULT_MAX_ATTEMPTS = 3`
- `DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "workflow.schema.json"`
- `class WorkflowEditError(ValueError)`: every refusal in this module raises it.

**The edit shapes.** An edit is one JSON object. Its key set must be EXACTLY
`{"op", "node"}` plus the op's own keys:

| `op` | its own keys | value rules |
|---|---|---|
| `node_order` | `index` | `int` (a `bool` is refused), `>= 0` |
| `lane_pin` | `lane` | `str` matching `^[A-Za-z0-9][A-Za-z0-9_.:-]*$` |
| `tool_grant` | `group`, `tool`, `grant` | `group` in `TOOL_GROUPS`; `tool` is a `str`, non-empty after `.strip()`; `grant` is a `bool` |
| `retry_count` | `max_attempts` | `int` (a `bool` is refused), `RETRY_MIN <= v <= RETRY_MAX` |

`node` is always a non-empty `str`.

**Functions (exact signatures):**

1. `parse_edit(raw: str | Mapping[str, Any]) -> dict[str, Any]`
   - A `str` is decoded with `json.loads`. Invalid JSON raises `WorkflowEditError`.
   - A `list`, whether passed in or decoded, raises `WorkflowEditError` whose
     message contains `one edit per candidate`. A candidate that changes two
     things cannot be attributed.
   - Any other non-mapping raises `WorkflowEditError`.
   - When `op` is missing or not in `OPS`, it raises with a message containing `unknown op`.
   - A missing key, an extra key or a value-rule violation raises `WorkflowEditError`.
   - It returns a NEW plain `dict` with exactly the allowed keys. `tool` is
     stored stripped, and every other value is unchanged.
2. `edit_signature(edit: str | Mapping[str, Any]) -> str`. It returns
   `hashlib.sha256(json.dumps(parse_edit(edit), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]`.
3. `apply_edit(document: Mapping[str, Any], edit: str | Mapping[str, Any]) -> dict[str, Any]`
   - It parses the edit, then works on `copy.deepcopy(dict(document))`. The
     input is never mutated.
   - `document["nodes"]` must be a `list`, or it raises `WorkflowEditError`.
   - The target is the node dict whose `name` equals `edit["node"]` exactly.
     When there is none, it raises with a message containing `unknown node`.
   - `node_order`: let `i` be the node's current index.
     - `index >= len(nodes)` raises.
     - `index == i` raises with a message containing `no-op`.
     - Otherwise it runs `nodes.insert(index, nodes.pop(i))`.
   - `lane_pin`:
     - A node whose `type` is not in `LANE_NODE_TYPES` raises with a message
       containing `cannot pin a lane`.
     - `node.get("model_lane") == lane` raises with `no-op`.
     - Otherwise it sets `node["model_lane"] = lane`.
   - `tool_grant`: read `tools = node.get("tools")`.
     - A `list` raises with `list-form tools`. `None` becomes `{}`. Any other
       non-dict raises.
     - Let `current = list(tools.get(group) or [])`.
     - With `grant` true:
       - `tool in current` raises with `no-op`.
       - The tool must already appear in the `tools[group]` list of at least
         one OTHER node whose `tools` is a dict. Otherwise it raises with a
         message containing `new capability`. The loop may move an existing
         grant but never introduce one.
       - Then it appends the tool.
     - With `grant` false:
       - `tool not in current` raises with `no-op`.
       - Otherwise it removes every occurrence.
     - Finally it sets `tools[group] = current` and `node["tools"] = tools`.
       The other group is untouched, and an emptied group stays as `[]`.
   - `retry_count`: read `retry = node.get("retry")`.
     - `None` becomes `{}`. A non-dict raises.
     - `int(retry.get("max_attempts", DEFAULT_MAX_ATTEMPTS)) == max_attempts`
       raises with `no-op`.
     - Otherwise it sets `retry["max_attempts"] = max_attempts` and
       `node["retry"] = retry`. Other `retry` keys are preserved.
   - It returns the new document.
4. `schema_errors(document: Any, *, schema_path: str | Path | None = None) -> list[str]`
   - It loads the JSON schema from `schema_path`, or `DEFAULT_SCHEMA_PATH`
     when that is `None`, and validates with `validator_for(schema)(schema).iter_errors(document)`.
   - Each error renders as `f"{path}: {error.message}"`, where `path` is
     `"/".join(str(p) for p in error.absolute_path)`, or `"<root>"` when that
     is empty.
   - The list is sorted, and a valid document returns `[]`.
5. `render(document: Mapping[str, Any]) -> str`. It returns
   `yaml.safe_dump(dict(document), sort_keys=False, default_flow_style=False, allow_unicode=True)`.
6. `sha256_text(text: str) -> str`. It returns the hex sha256 of `text.encode("utf-8")`.
7. `file_sha256(path: str | Path) -> str`. It returns `sha256_text` of the
   file's utf-8 text, or `""` when the file does not exist.
8. `materialize(workflow_path: str | Path, edit: str | Mapping[str, Any], *, schema_path: str | Path | None = None) -> dict[str, Any]`
   - The steps run in this order. Each failure raises `WorkflowEditError`
     whose message STARTS with the quoted prefix:
     1. `parse_edit(edit)`. Its own errors propagate unchanged.
     2. It reads the file as utf-8. A missing file raises `workflow not found: `.
     3. It runs `yaml.safe_load`. A YAML error or a non-mapping raises
        `base workflow is not a YAML mapping`.
     4. A non-empty `schema_errors(base)` raises `base workflow violates the schema: `,
        followed by the first error.
     5. It runs `compile_workflow(workflow_path)`. A `WorkflowCompileError`
        raises `base workflow does not compile: `.
     6. It runs `after = apply_edit(base, edit)`.
     7. A non-empty `schema_errors(after)` raises `edited workflow violates the schema: `.
     8. It computes `after_text = render(after)`, writes it to
        `<d>/workflow.yaml` inside a `tempfile.TemporaryDirectory()` `d`, and
        runs `compile_workflow` on that file. A `WorkflowCompileError` raises
        `edited workflow does not compile: `.
     9. For `node_order` only: when the edited workflow's
        `topological_order` equals the base's, it raises
        `no-op: execution order unchanged`.
   - It returns a dict with EXACTLY these keys:
     - `edit`: the parsed edit;
     - `signature`: `edit_signature(edit)`;
     - `path`: `str(workflow_path)`;
     - `before_text` and `after_text`;
     - `before_sha256` and `after_sha256`, both from `sha256_text`;
     - `before_order` and `after_order`: each `list(compiled.topological_order)`.
   - It NEVER writes `workflow_path`.
9. `apply_to_file(workflow_path: str | Path, result: Mapping[str, Any]) -> bool`
   - When `file_sha256(workflow_path) != result["before_sha256"]`, it returns
     `False` and writes nothing.
   - Otherwise it writes `result["after_text"]` as utf-8 and returns `True`.
10. `rollback(workflow_path: str | Path, result: Mapping[str, Any]) -> bool`
    - When `file_sha256(workflow_path) != result["after_sha256"]`, it returns
      `False` and writes nothing. A file someone changed since is never clobbered.
    - Otherwise it writes `result["before_text"]` and returns `True`.

`__all__` lists the twelve public names: `OPS`, `WorkflowEditError`,
`parse_edit`, `edit_signature`, `apply_edit`, `schema_errors`, `render`,
`sha256_text`, `file_sha256`, `materialize`, `apply_to_file` and `rollback`.

## Files in scope (touch ONLY these)

- `mini_ork/learning/workflow_edit.py` (new)
- `tests/unit/test_workflow_edit.py` (new)

Do NOT modify any other file. In particular, do not touch
`mini_ork/cli/apply.py`, `mini_ork/learning/probe_scorer.py` or any recipe.

## Tests (`tests/unit/test_workflow_edit.py`: hermetic, tmp_path only, no DB, no network)

Write this fixture verbatim to `tmp_path / "workflow.yaml"`. It passes the
real schema and compiles, and its topological order is
`planner, implementer, lint, docs, publisher`:

```
version: "0.1.0"
task_class: fw_edit
nodes:
  - name: planner
    type: planner
    dispatch_mode: serial
    model_lane: sonnet
    prompt_ref: prompts/planner.md
  - name: implementer
    type: implementer
    dispatch_mode: serial
    model_lane: sonnet
    prompt_ref: prompts/implementer.md
    tools:
      native: [Read, Write]
      mcp: [codegraph]
    retry:
      max_attempts: 3
  - name: lint
    type: verifier
    dispatch_mode: serial
    verifier_ref: verifiers/lint.py
    tools:
      native: [Read, Bash]
  - name: docs
    type: researcher
    dispatch_mode: serial
    model_lane: sonnet
    prompt_ref: prompts/docs.md
    tools:
      native: [Read]
  - name: publisher
    type: publisher
    dispatch_mode: serial
    prompt_ref: null
edges:
  - {from: planner, to: implementer, edge_type: depends_on}
  - {from: planner, to: docs, edge_type: depends_on}
  - {from: implementer, to: lint, edge_type: depends_on}
  - {from: lint, to: publisher, edge_type: depends_on}
  - {from: docs, to: publisher, edge_type: depends_on}
```

Tests:

- **parse_edit refusals.** Each raises `WorkflowEditError`:
  - a list of two edits (the message contains `one edit per candidate`);
  - `{"op": "rename", "node": "x"}` (the message contains `unknown op`);
  - a `lane_pin` with an extra key `"why"`;
  - a `retry_count` with `max_attempts` of `0`, of `6` and of `True`;
  - a `tool_grant` with `group: "shell"`;
  - the string `"not json"`.
- **parse_edit accepts a JSON string** and returns a dict equal to the decoded
  mapping. `" Bash "` comes back as `"Bash"`.
- **edit_signature** is 16 hex chars, is equal for the same edit given as a
  dict and as a JSON string with keys in another order, and differs when `lane` differs.
- **apply_edit never mutates its input.** After any successful edit, the
  original `yaml.safe_load` dict is unchanged.
- **lane_pin.**
  - `implementer → "glm"` sets `model_lane` to `glm`.
  - `lint` (verifier) and `publisher` raise with `cannot pin a lane`.
  - `implementer → "sonnet"` raises with `no-op`.
- **tool_grant.**
  - Granting `Bash` to `implementer`/`native` works, because `lint` has it.
  - Granting `Edit` to `implementer`/`native` raises with `new capability`.
  - Granting `codegraph` to `docs`/`mcp` works. `docs.tools` is then
    `{"native": ["Read"], "mcp": ["codegraph"]}`.
  - Revoking `Write` from `implementer` leaves `native == ["Read"]` and `mcp` unchanged.
  - Revoking `Edit` from `implementer` raises with `no-op`.
- **retry_count.**
  - `implementer → 2` gives `retry == {"max_attempts": 2}`.
  - `implementer → 3` raises with `no-op`.
  - `planner → 3` raises with `no-op`, because the default is 3.
  - `planner → 1` creates `retry: {max_attempts: 1}`.
- **node_order via materialize.**
  - `{"op": "node_order", "node": "docs", "index": 1}` returns
    `after_order == ["planner", "docs", "implementer", "lint", "publisher"]`
    and `before_order == ["planner", "implementer", "lint", "docs", "publisher"]`.
  - `{"op": "node_order", "node": "publisher", "index": 0}` raises with a
    message starting `no-op: execution order unchanged`.
  - `index: 9` raises.
- **materialize result shape.**
  - The keys are exactly the nine listed.
  - `before_sha256 == file_sha256(path)`.
  - `after_sha256 == sha256_text(after_text)`.
  - `yaml.safe_load(after_text)` shows the edit.
  - The file bytes on disk are unchanged.
- **materialize refusals.**
  - A missing path raises with `workflow not found: `.
  - A fixture without the `edges` key raises with
    `base workflow violates the schema: `.
- **apply_to_file / rollback round trip.**
  - `apply_to_file` returns `True`, and the file then has
    `sha256 == after_sha256`.
  - A second `apply_to_file` returns `False`.
  - `rollback` returns `True`, and the file bytes equal the original.
  - After rollback, a further `rollback` returns `False`.
  - Changing the file by hand after `apply_to_file` makes `rollback` return
    `False`, and the hand edit survives.
- **schema_errors** is `[]` for the fixture. It is non-empty for the fixture
  with `nodes[0]["type"] = "wizard"`, and each entry contains `": "`.

## Verification commands

- `pytest -q tests/unit/test_workflow_edit.py`
- `pytest -q tests/unit/test_apply_harness_targets.py tests/unit/test_harness_operator_py.py tests/unit/test_recipe_graph_compile.py tests/unit/test_workflow_schema_recursion.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_workflow_edit.py --junitxml="${MINI_ORK_RUN_DIR}/workflow-edit-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/workflow-edit-junit.xml`

## Success criteria

- Both verification commands pass with every pre-existing test unchanged.
- `ruff check --select F,E9 mini_ork/learning/workflow_edit.py` is clean.
- `git diff --stat` lists only the two files in scope.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
