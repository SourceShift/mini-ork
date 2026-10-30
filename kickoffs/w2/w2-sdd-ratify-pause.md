# W2-SDD-ratify-b: spec-driven-delivery pauses after `contract_compiler` so an operator can ratify

## Goal

This is SDD option A, the operator ratification gate between `contract_compiler` and
`ratification_check` (`docs/audits/2026-10-16-session-gaps/mini-ork-cb.md`). The operator
command already exists: `mini-ork specs ratify <run_id> --list` and
`specs ratify <run_id> --spec <id> (--all | --entry N) --note TEXT` (`mini_ork/cli/specs.py`).
What is missing is the **pause**. Today a clean spec dispatches before any human has seen the
compiled contract, and a spec with a real gap fails `ratification_check` before anyone gets to
ratify it.

mini-ork already has per-node review pauses:
- `mini_ork/gates/review_policy.py` provides `load_policy` and `after_nodes`, read from
  `MO_REVIEW_AFTER_NODES` and `<home>/config/review.yaml`.
- `mini_ork/gates/node_review.py` and `mini_ork/gates/intervention_gate.py` park the run after a
  matching node.
- `mini-ork approvals approve|reject <run_id> --node <node>` resumes it
  (`mini_ork/cli/approvals_cmd.py`).

That policy is global, though. This kickoff lets a **workflow declare** its own review nodes, the
same way a `recursion:` block publishes `MO_RECURSION_*` in `mini_ork/cli/execute.py`. It also
makes spec-driven-delivery declare `contract_compiler`.

(Per-node `gates:` were removed in eco6 K13 because nothing enforced them. This declaration is
enforced through the existing review-policy machinery, which is the point.)

Read first:
- `mini_ork/cli/execute.py`: the `if workflow and os.path.isfile(workflow):` block (where
  `load_recursion_config` and `publish_env` are used), `compiled.nodes`, and how `main` returns
  error codes.
- `mini_ork/gates/review_policy.py` (`load_policy`, `_coerce_after_nodes`).
- `mini_ork/context.py` (`publish_env`, which writes both the contextvar layer and `os.environ`).
- `schemas/workflow.schema.json` (`additionalProperties: false`; the `recursion` property shape).
- `tests/unit/test_eco6_k13_node_gates_removed.py` and `tests/unit/test_eco6_k3_recipe_schema.py`,
  which schema-check every recipe.
- `recipes/spec-driven-delivery/workflow.yaml` and `README.md`.

## Mechanism (exact spec)

### `schemas/workflow.schema.json`

Add a top-level property:

```json
"review_after_nodes": {
  "type": "array",
  "description": "Nodes after which a run parks for operator review (merged into the review policy's after_nodes; MO_REVIEW_RECIPE_NODES=0 disables).",
  "items": {"type": "string", "minLength": 1},
  "uniqueItems": true
}
```

### `mini_ork/cli/execute.py`

1. Add a module-level helper:

```python
def _declared_review_nodes(workflow_path, node_names, environ) -> tuple[tuple[str, ...], str]:
    """``(after_nodes_to_publish, message)`` for a workflow's ``review_after_nodes``.

    ``()`` when the workflow declares none, or when ``MO_REVIEW_RECIPE_NODES=0``
    (the message says so). A declared name that is not a node of this workflow
    raises ``ValueError`` (zero-fallback: a typo must not silently drop the
    pause). Otherwise the result is the review policy's current ``after_nodes``
    (``load_policy(env=environ)``, so env and ``review.yaml`` still count),
    followed by the declared nodes, de-duplicated, order preserved.
    """
```

   Read the YAML with `yaml.safe_load`, the same way the module reads workflows elsewhere.
   Accept only a list of strings; anything else raises `ValueError`.
2. In the `if workflow and os.path.isfile(workflow):` block, after the recursion publish, call
   `_declared_review_nodes(workflow, set(compiled.nodes), os.environ)`.
   - **On `ValueError`:** print `execute: workflow review_after_nodes: <message>` to stderr and
     return 2 from `main`. Use the same early-return style the function already uses for a fatal
     setup error.
   - **When the tuple is non-empty:** `publish_env({"MO_REVIEW_AFTER_NODES": ",".join(nodes)})` and
     print `[review] pausing after: <declared nodes> (declared by the workflow;
     MO_REVIEW_RECIPE_NODES=0 disables)` to stderr.
   - **When the knob disabled it:** print the message.

### `recipes/spec-driven-delivery/workflow.yaml`

Add top-level `review_after_nodes: [contract_compiler]`.

### `recipes/spec-driven-delivery/README.md`

Add a section `## Ratifying the compiled contract` covering:
- The run parks after `contract_compiler`.
- Inspect `<run_dir>/spec-cards/` and `mini-ork specs ratify <run_id> --list`.
- Acknowledge real gaps with `mini-ork specs ratify <run_id> --spec <id> (--entry N | --all) --note "…"`.
- Continue with `mini-ork approvals approve <run_id> --node contract_compiler`, which resumes the
  run.
- To adjust, run `mini-ork approvals reject <run_id> --node contract_compiler --note "…"`, edit
  the specs, and start a new run. Re-running only the compiler would trip
  `ratification_check`'s source-hash drift check.
- Unattended runs set `MO_REVIEW_RECIPE_NODES=0`.

## Files in scope (touch ONLY these)

- `schemas/workflow.schema.json`
- `mini_ork/cli/execute.py`
- `recipes/spec-driven-delivery/workflow.yaml`
- `recipes/spec-driven-delivery/README.md`
- `tests/unit/test_review_after_nodes_decl_py.py` (new)

Do NOT modify any other file.

## Tests (`tests/unit/test_review_after_nodes_decl_py.py`, hermetic)

`_declared_review_nodes`, with workflows written to `tmp_path`, `environ` dicts, and
`MINI_ORK_HOME` pointed at an empty `tmp_path` home so no real `review.yaml` leaks in:
- No key gives `()`.
- `[contract_compiler]` with node names `{contract_compiler, x}` and an empty environ gives
  `("contract_compiler",)`.
- `MO_REVIEW_AFTER_NODES=reviewer` in environ gives `("reviewer", "contract_compiler")`.
- `MO_REVIEW_RECIPE_NODES=0` gives `()`, and the message mentions the knob.
- An unknown node `[nope]` raises `ValueError` naming `nope`.
- A non-list value (`review_after_nodes: contract_compiler`) raises `ValueError`.

Schema:
- `jsonschema` validation accepts `review_after_nodes: [a]`.
- It rejects `review_after_nodes: "a"` and `[a, a]`.

Recipe:
- `recipes/spec-driven-delivery/workflow.yaml` validates against the schema, declares
  `contract_compiler`, and `contract_compiler` is one of its node names.

`main` wiring: drive `mini_ork.cli.execute.main` the way `tests/unit/test_revise_loop.py::_run`
does, with an overlay workflow declaring `review_after_nodes: [nope]`. Assert rc == 2 and that
stderr names `nope`, and that no node was dispatched.

## Verification commands

- `python3 -m pytest -q -p no:asyncio tests/unit/test_review_after_nodes_decl_py.py tests/unit/test_eco6_k13_node_gates_removed.py tests/unit/test_eco6_k3_recipe_schema.py tests/unit/test_eco6_k16_recursion_keys.py tests/unit/test_eco6_k14_edge_types.py`
- `python3 -m pytest -q -p no:asyncio tests/unit/test_revise_loop.py tests/unit/test_recursion_regression_py.py -k "not slow"`
- `python3 -m pytest -q -p no:asyncio -k "review_policy or node_review or intervention or approvals" tests/unit`
- `ruff check --select F,E9 mini_ork/cli/execute.py tests/unit/test_review_after_nodes_decl_py.py`

## Self-application measurement

```measure
python3 -m pytest -q -p no:asyncio tests/unit/test_review_after_nodes_decl_py.py --junitxml="${MINI_ORK_RUN_DIR}/w2-sdd-ratify-pause-junit.xml"
```

## Required artifacts: declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/w2-sdd-ratify-pause-junit.xml`

## Success criteria

- All verification commands pass, and every pre-existing test is unchanged.
- A workflow without `review_after_nodes` behaves exactly as today.
- spec-driven-delivery parks after `contract_compiler` unless `MO_REVIEW_RECIPE_NODES=0`.

## Rules

Edit files directly; do not emit unified diffs. Execute this spec exactly; do not
research and do not add lens or drafter nodes.
