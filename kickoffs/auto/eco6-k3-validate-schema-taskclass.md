# ECOSYSTEM §6 · K3: validate checks the workflow schema and task-class uniqueness

## Goal

Two recipe-level problems pass `mini-ork validate` today:

1. **The workflow schema is never checked.** `mini_ork/cli/validate.py`
   (`_check_recipe`) only checks that `workflow.yaml` is YAML and that the
   `recursion:` block loads. Checked against `schemas/workflow.schema.json`,
   five recipes fail right now:
   - `code-fix`, `epic-runner`, `framework-edit`, `registry-audit`: their
     `retries` edges declare `max_rounds`, which the compiler reads
     (`mini_ork/workflow/compiler.py`, `edge.get("max_rounds", 2)`) but the
     edge schema does not allow.
   - `session-task-judge`: its `workflow.yaml` has `name:` instead of
     `task_class:`, and `version: 1` is an integer, not a string.
   Without this check, later schema removals cannot be enforced either.
2. **Three recipes declare the same task class.** `recipes/refactor-audit`,
   `recipes/bug-audit-cmgk` and `recipes/feature-inventory-cmgk` have
   byte-identical `task_class.yaml` files (`name: refactor_audit`, same
   keywords). Both cmgk `workflow.yaml` and `artifact_contract.yaml` files also
   say `task_class: refactor_audit`, and so does
   `recipes/bug-audit-fe-be/artifact_contract.yaml` (its workflow says
   `fe_be_bug_audit`). `validate._infer_recipe` maps the class to
   `refactor-audit`, so classification can never reach the cmgk recipes, and
   learning keyed by task class mixes three recipes.

All paths are relative to the repository root.

## Mechanism (exact spec)

### A. Schema

1. `schemas/workflow.schema.json`: add an optional `max_rounds` property to the
   edge definition: `{"type": "integer", "minimum": 0, "description": "retries
   edges only: maximum revise rounds this edge may fire (compiler default 2)"}`.
   Change nothing else in the schema.
2. `recipes/session-task-judge/workflow.yaml`: replace the top-level
   `name: session_task_judge` with `task_class: session_task_judge` and quote
   the version (`version: "1"`). No other line changes.
3. `mini_ork/cli/validate.py`, in `_check_recipe`, when `workflow.yaml` is
   valid YAML: validate the document against `schemas/workflow.schema.json`
   with `jsonschema` (use the validator class matching the schema's `$schema`,
   draft 2020-12). Report each error as
   `findings.error(f"recipe {recipe} workflow.yaml violates the schema at <json path>: <message>", f"fix {wf} or schemas/workflow.schema.json")`,
   at most 5 errors per recipe, then one summary line if there were more.
   If `jsonschema` cannot be imported, add one **warning** saying the schema
   check was skipped (it is a declared dependency, so this should not happen).

### B. Task-class uniqueness

1. Rename the duplicated classes:
   - `recipes/bug-audit-cmgk/{task_class,workflow,artifact_contract}.yaml`:
     `refactor_audit` → `bug_audit_cmgk`.
   - `recipes/feature-inventory-cmgk/{task_class,workflow,artifact_contract}.yaml`:
     `refactor_audit` → `feature_inventory_cmgk`.
   - `recipes/bug-audit-fe-be/artifact_contract.yaml`: `refactor_audit` →
     `fe_be_bug_audit`.
2. Give each cmgk `task_class.yaml` its own description and keywords:
   - `bug_audit_cmgk`: description "Four-family (codex/minimax/glm/kimi) bug
     audit of a codebase; ranked bug findings, report only." Keywords:
     `bug audit cmgk`, `cmgk bug audit`, `four-family bug audit`.
   - `feature_inventory_cmgk`: description "Four-family inventory of the
     features a codebase ships, with code anchors; report only." Keywords:
     `feature inventory`, `feature inventory cmgk`, `inventory features`.
   - Neither list may contain any keyword from `recipes/refactor-audit/task_class.yaml`.
   - Leave `recipes/refactor-audit/` unchanged.
3. `mini_ork/cli/validate.py`: add `_check_task_class_names(root, findings)`,
   called from `main` on every invocation, after `_check_kickoff`. For every
   `recipes/*/task_class.yaml`, read `name`. Each name that appears in more than
   one recipe is an **error**:
   `task class '<name>' is declared by N recipes: <a>, <b>, …`, with the fix
   hint `give each recipe a distinct name in its task_class.yaml`. For each
   recipe, when `workflow.yaml` or `artifact_contract.yaml` declares a
   `task_class` different from that recipe's `task_class.yaml` `name`, add a
   **warning** naming the file. Recipes with no `task_class.yaml` are skipped.

## Files in scope (touch ONLY these)

- `schemas/workflow.schema.json`
- `mini_ork/cli/validate.py`
- `recipes/session-task-judge/workflow.yaml`
- `recipes/bug-audit-cmgk/task_class.yaml`, `workflow.yaml`, `artifact_contract.yaml`
- `recipes/feature-inventory-cmgk/task_class.yaml`, `workflow.yaml`, `artifact_contract.yaml`
- `recipes/bug-audit-fe-be/artifact_contract.yaml`
- `tests/unit/test_eco6_k3_recipe_schema.py` (new)

Do NOT modify any other recipe, the compiler, or `classify.py`.

## Tests (`tests/unit/test_eco6_k3_recipe_schema.py`, hermetic)

- **Every shipped recipe passes the schema.** For each
  `recipes/*/workflow.yaml`, `jsonschema` validation against
  `schemas/workflow.schema.json` yields no errors.
- **Task classes are unique and consistent.** Across `recipes/*/task_class.yaml`
  every `name` is unique, and for each recipe the `task_class` in
  `workflow.yaml` and `artifact_contract.yaml` (when present) equals it.
- **validate reports a schema violation.** Copy `recipes/code-fix` to a tmp
  root, add `bogus_key: 1` to one edge, run
  `validate.main(["--recipe", "code-fix"])` with `MINI_ORK_ROOT` pointing at the
  tmp root (and `MINI_ORK_HOME` at an empty tmp home): rc is 1 and stderr
  mentions `violates the schema`.
- **validate reports a duplicate task class.** In a tmp root with two recipe
  dirs whose `task_class.yaml` share `name: dup_class`, `main([])` returns 1 and
  stderr contains `declared by 2 recipes`.
- **classification still routes refactor kickoffs to refactor-audit.**
  `mini_ork.cli.validate._infer_recipe` on a kickoff file containing
  "refactor audit of the provider roster" returns `refactor-audit`.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k3_recipe_schema.py tests/unit/test_cli_validate_py.py tests/unit/test_workflow_schema_recursion.py tests/unit/test_mini_ork_classify_py.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k3_recipe_schema.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k3-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k3-junit.xml`

## Success criteria

- The verification command passes with every pre-existing test unchanged.
  (If `tests/unit/test_mini_ork_classify_py.py` does not exist, drop it from the
  command; do not create it.)
- `ruff check --select F,E9 mini_ork/cli/validate.py` is clean.
