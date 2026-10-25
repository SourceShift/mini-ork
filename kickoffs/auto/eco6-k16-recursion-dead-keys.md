# ECOSYSTEM §6 · K16: drop the three `recursion:` keys nothing reads

## Goal

A `recursion:` block must declare five keys (`mini_ork/workflow/recursion.py`
`FIELDS`, all-or-none), and `mini_ork/cli/execute.py` exports all five as
`MO_RECURSION_*`. Only two have a reader:

| Key | Reader |
|---|---|
| `max_iterations` | `workflow/compiler.py` (repair-edge cap; honored since eco6 K2) and `recipes/goal-loop/lib/drive.py` |
| `budget_cap_total_usd` | `execute.py` revise loop (since eco6 K2) and `goal-loop/lib/drive.py` |
| `budget_cap_per_iter_usd` | none (exported, read by nothing) |
| `convergence_check` | none — a free-text label; the loop stops when the verifier passes |
| `divergence_kill` | none — multi-line prose; goal-loop uses its coded `divergence()` |

Five recipes declare the block: `goal-loop`, `doc-to-features-loop`,
`prompt-graph-loop`, `recursive-validate-impl`, `spec-driven-delivery`.
Remove the three unread keys so the block only promises what runs.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. `mini_ork/workflow/recursion.py`: `FIELDS = ("max_iterations", "budget_cap_total_usd")`;
   `RecursionConfig` keeps only those two fields; the loader requires exactly
   those two. A block that still contains `budget_cap_per_iter_usd`,
   `convergence_check` or `divergence_kill` raises `RecursionConfigError` with
   `recursion.<key> was removed in 2026-10 (nothing read it); delete it from <path>`
   — keep the module's existing unknown-key check and extend its message for
   these three names. Update docstrings ("the two declared caps").
2. `mini_ork/cli/execute.py`: in the block that exports `MO_RECURSION_*`, keep
   `MO_RECURSION_MAX_ITERATIONS` and `MO_RECURSION_BUDGET_CAP_TOTAL_USD`; delete
   the `MO_RECURSION_CONVERGENCE_CHECK`, `MO_RECURSION_BUDGET_CAP_PER_ITER_USD`
   and `MO_RECURSION_DIVERGENCE_KILL` lines. Nothing else in the file changes.
3. `schemas/workflow.schema.json`: the `recursion` object's `properties` and
   `required` list only the two kept keys (`additionalProperties: false`).
4. The five recipes' `workflow.yaml`: delete the three keys from the
   `recursion:` block. Preserve their text as YAML comments directly above the
   block (`# convergence (informational): …`, `# divergence (informational): …`)
   so the authors' intent is not lost. No other line changes.
5. `docs/operator/feature-flags.md`: remove the three `MO_RECURSION_*` rows (around
   lines 248-268) and add one sentence that a `recursion:` block now declares
   `max_iterations` and `budget_cap_total_usd` only.

## Files in scope (touch ONLY these)

- `mini_ork/workflow/recursion.py`
- `mini_ork/cli/execute.py` (the three export lines only)
- `schemas/workflow.schema.json` (recursion object only)
- `recipes/goal-loop/workflow.yaml`, `recipes/doc-to-features-loop/workflow.yaml`,
  `recipes/prompt-graph-loop/workflow.yaml`, `recipes/recursive-validate-impl/workflow.yaml`,
  `recipes/spec-driven-delivery/workflow.yaml`
- `docs/operator/feature-flags.md`
- `tests/unit/test_workflow_schema_recursion.py`, `tests/unit/test_workflow_recursion_config_py.py`,
  `tests/unit/test_goal_loop_recipe.py` (update cases that pin the removed keys only)
- `tests/unit/test_eco6_k16_recursion_keys.py` (new)

Do NOT modify `recipes/goal-loop/lib/drive.py` (it reads only the two kept
`MO_RECURSION_*` variables) or `mini_ork/features/registry.py`.

## Tests (`tests/unit/test_eco6_k16_recursion_keys.py`, hermetic)

- `load_recursion_config` on each of the five shipped recipes returns a config
  with exactly `max_iterations` and `budget_cap_total_usd` (values unchanged
  from before: 30/150.0, 8/100.0, 5/20.0, 5/60.0, 6/100.0 for goal-loop,
  doc-to-features-loop, prompt-graph-loop, recursive-validate-impl,
  spec-driven-delivery — read the real values from git HEAD in the test).
- A tmp workflow whose block still has `convergence_check` → `RecursionConfigError`
  whose message contains `was removed in 2026-10`.
- A block missing `budget_cap_total_usd` → `RecursionConfigError` naming it.
- The schema's recursion object has exactly the two properties, both required.
- The text of `mini_ork/cli/execute.py` contains none of
  `MO_RECURSION_CONVERGENCE_CHECK`, `MO_RECURSION_BUDGET_CAP_PER_ITER_USD`,
  `MO_RECURSION_DIVERGENCE_KILL`, and still contains the two kept names.
- All five recipes compile and validate against the schema.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k16_recursion_keys.py tests/unit/test_workflow_schema_recursion.py tests/unit/test_workflow_recursion_config_py.py tests/unit/test_goal_loop_recipe.py tests/unit/test_eco6_k3_recipe_schema.py tests/unit/test_eco6_k2_revise_caps.py tests/unit/test_revise_loop.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_eco6_k16_recursion_keys.py --junitxml="${MINI_ORK_RUN_DIR}/eco6-k16-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/eco6-k16-junit.xml`

## Success criteria

- The verification command passes (drop any listed test file that does not
  exist; do not create it).
- `ruff check --select F,E9 mini_ork/workflow/recursion.py mini_ork/cli/execute.py` is clean.
- The five recipes' `max_iterations` and `budget_cap_total_usd` values are
  byte-identical to before.
