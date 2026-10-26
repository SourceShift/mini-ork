# ECOSYSTEM §6 · K13: remove per-node `gates:` (never enforced) and node `max_cost_usd`

## Goal

347 nodes in 42 of the 48 recipes declare `gates:` in `workflow.yaml`
(`budget_gate` 322, `scope_gate` 37, `deployment_gate` 2). Nothing enforces them:

- the compiler's node type (`mini_ork/workflow/compiler.py`, `WorkflowNode`)
  has no `gates` field and `_node_from_yaml` never reads the key;
- the executor, the handlers and the publisher never read it;
- wiring it up would not help: `gate_registry.gate_evaluate` looks gates up by
  registered **id** and fails unknown ids, so passing these names through would
  fail all 347 nodes; dispatching by **type** would be a no-op (`budget_gate`
  needs a limit no node declares, `scope_gate` passes trivially for the recipe's
  own class, `deployment_gate` needs an executable path).
- The real gates already run per task class (`cli/verify.py` →
  `gate_registry.gate_run_all`), the run budget is `MO_ORCH_BUDGET_USD`, and
  per-node checks are verifier/reviewer nodes.

Yet the UI shows them as checks and `docs/EXTENSION.md` says "Gates fire in
declaration order". Remove the key everywhere so nobody believes it works.
Node `max_cost_usd` is the same: in the schema, used by 0 recipes, read by
nothing.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. **Codemod, checked in and run.** Write
   `scripts/codemods/eco6_strip_node_gates.py` that, for every
   `recipes/*/workflow.yaml`, removes the `gates` key from each **node** (both
   the inline flow form `{ …, gates: [a, b], … }` and the block form
   `    gates:\n      - a\n      - b`) and nothing else. It must preserve every
   other byte: comments, ordering, quoting, blank lines (do a line/regex-level
   edit, not a YAML load/dump). It must not touch top-level keys or
   `task_class.yaml` `default_gates`. Run it once; commit the script and the
   resulting recipe edits. After it runs, every edited file must still load with
   `yaml.safe_load` and compile with `mini_ork.workflow.compile_workflow`, and a
   YAML-level comparison must show the node dicts equal to before minus `gates`.
2. **Schema** (`schemas/workflow.schema.json`): delete the node `gates` and
   `max_cost_usd` properties.
3. **Types** (`mini_ork/types.py`): delete `NodeSpec.gates` and its
   serialization (lines with `self.gates` / `data["gates"]`).
4. **UI** — stop rendering per-node gates:
   - `mini_ork/web/recipes.py`, `mini_ork/web/agents.py`,
     `mini_ork/web/routes/dispatch.py`: drop the `"gates": …` entry from the
     node dicts they build.
   - `mini_ork/acp/recipe_view.py`: drop the `"gates"` field and the loop that
     renders each gate as a "gate:…" check.
   - `mini_ork/ide_pages/run.py`: drop the node `gates` field, its parsing and
     the `node_dict["gates"]` display line.
5. **validate** (`mini_ork/cli/validate.py`, `_check_recipe`): before the schema
   check, if any node declares `gates`, report one **error** per recipe:
   `recipe <r> node '<n>' declares gates: — per-node gates were never enforced`
   with the hint `remove it; add a verifier node, or register a task-class gate with mini_ork.gates.gate_registry.gate_register()`.
6. **Docs** (`docs/EXTENSION.md`):
   - remove the `gates:` lines from the example node;
   - section "6. Custom Gates" still documents the deleted bash
     `lib/gate_registry.sh`. Rewrite it for the Python API that exists:
     `mini_ork.gates.gate_registry.register_gate_evaluator(type, fn)` to add a
     gate type, and `gate_registry.gate_register(db, gate_type, condition)` to
     register a gate row (check the real signatures in
     `mini_ork/gates/gate_registry.py` and quote them exactly);
   - replace the "Reference in `workflow.yaml`" example and the "Gates fire in
     declaration order…" sentence with: registered gates run per task class
     when `mini-ork verify` calls `gate_run_all`; to check one node's output,
     add a verifier node.

## Files in scope (touch ONLY these)

- `scripts/codemods/eco6_strip_node_gates.py` (new)
- `recipes/*/workflow.yaml` — only the edits the codemod makes
- `schemas/workflow.schema.json`
- `mini_ork/types.py`
- `mini_ork/web/recipes.py`, `mini_ork/web/agents.py`, `mini_ork/web/routes/dispatch.py`
- `mini_ork/acp/recipe_view.py`, `mini_ork/ide_pages/run.py`
- `mini_ork/cli/validate.py`
- `docs/EXTENSION.md`
- tests that assert per-node gates in those renderers (e.g.
  `tests/unit/test_acp_recipe_view.py`): remove only the gate assertions
- `tests/unit/test_eco6_k13_node_gates_removed.py` (new)

## Tests (`tests/unit/test_eco6_k13_node_gates_removed.py`, hermetic)

- No `recipes/*/workflow.yaml` node has a `gates` key (load each with
  `yaml.safe_load`), and every one validates against
  `schemas/workflow.schema.json` with `jsonschema`.
- Every recipe still compiles: `compile_workflow(path)` succeeds for each.
- The schema's node definition has neither `gates` nor `max_cost_usd`.
- **Codemod is byte-preserving.** On a tmp file with an inline-form node, a
  block-form node, a comment between nodes and a top-level key named `gates`
  (not under a node), the codemod removes only the two node keys; the comment
  and the top-level key survive byte-for-byte.
- **validate flags a stale recipe.** A tmp recipe whose node declares
  `gates: [budget_gate]` → `validate.main(["--recipe", …])` rc 1 with
  `per-node gates were never enforced` on stderr.
- `mini_ork.types.NodeSpec` has no `gates` attribute.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k13_node_gates_removed.py tests/unit/test_eco6_k3_recipe_schema.py tests/unit/test_cli_validate_py.py tests/unit/test_acp_recipe_view.py $(git grep -l -E "web\.recipes|web\.agents|routes\.dispatch|ide_pages\.run\b|ide_pages import run|types import NodeSpec|NodeSpec" -- tests | tr '\n' ' ')`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k13_node_gates_removed.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k13-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k13-junit.xml`

## Success criteria

- The verification command passes; no pre-existing assertion changes except
  removed per-node-gate assertions.
- `ruff check --select F,E9` on every changed `.py` file is clean.
- `git diff --stat -- recipes/` shows only `workflow.yaml` files, each with
  deletions only.
