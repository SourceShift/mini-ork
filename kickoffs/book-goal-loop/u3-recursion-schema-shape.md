# U3 workflow schema: give `recursion` a real shape

## Goal

- Problem: `schemas/workflow.schema.json` declares the top-level `recursion` block as
  `{"type": "object", "additionalProperties": true}` — any key, any typo, validates.
  Three recipes already ship recursion blocks (`recipes/doc-to-features-loop`,
  `recipes/prompt-graph-loop`, `recipes/recursive-validate-impl`) and all three use the
  exact same five keys. Lock the schema to that observed contract so a misspelled key
  (`budget_cap_total` vs `budget_cap_total_usd`) fails validation instead of silently
  doing nothing.
- Edit ONLY the `recursion` property in `schemas/workflow.schema.json`:

  ```json
  "recursion": {
    "type": "object",
    "description": "Optional bounded recursion configuration for recursive recipes.",
    "additionalProperties": false,
    "properties": {
      "max_iterations": { "type": "integer", "minimum": 1 },
      "convergence_check": { "type": "string" },
      "budget_cap_per_iter_usd": { "type": "number", "minimum": 0 },
      "budget_cap_total_usd": { "type": "number", "minimum": 0 },
      "divergence_kill": { "type": "string" }
    }
  }
  ```

  No key is `required` (partial blocks stay legal; the block itself remains optional).
- Add unit test `tests/unit/test_workflow_schema_recursion.py`:
  1. Load `schemas/workflow.schema.json`; for each of the three recipes above, load its
     `workflow.yaml` with `yaml.safe_load` and assert
     `jsonschema.validate(instance=workflow, schema=schema)` raises nothing.
  2. Negative case: take one valid workflow dict, add `recursion["budget_cap_total"] = 5`
     (typo key) and assert `jsonschema.ValidationError` is raised.
  3. Negative case: `recursion["max_iterations"] = 0` → `ValidationError`.
- Caution: if full-workflow validation trips on unrelated schema strictness, scope the
  positive tests to validating each recipe's `recursion` sub-dict against the schema's
  `properties.recursion` sub-schema instead — the point under test is the recursion
  shape, not the whole document.
- Size: **S** (one schema hunk + one new test file).

## Files in scope

- `schemas/workflow.schema.json`
- `tests/unit/test_workflow_schema_recursion.py` (new)

## Definition of Done

- `recursion` in the schema has `additionalProperties: false` and exactly the five
  properties listed above with the listed types/minimums.
- All three existing recipe recursion blocks validate unchanged (proven by the test).
- Typo-key and zero-iteration negative cases are rejected (proven by the test).
- No recipe YAML is modified; no other schema property is modified.

## Verification commands

- `python3 -m pytest tests/unit/test_workflow_schema_recursion.py -q`
- `python3 -c "import json; json.load(open('schemas/workflow.schema.json'))"`
- `python3 -m ruff check tests/unit/test_workflow_schema_recursion.py --select F,E9`

## Done When

- All verification commands pass in the isolated worktree.
- Reviewer node reports pass; diff touches only the two in-scope files.
