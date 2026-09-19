# U1 Recipe-local register.py loader

## Goal

- Primitive: let a recipe directory ship an optional `register.py` that self-registers
  extensions (node handlers, implementer submodes, routing policies, gate evaluators)
  when the recipe is executed — no core-module edits needed to add recipe-specific
  behavior. This closes the "core-edit tax": today `_IMPLEMENTER_SUBMODES` and
  `NODE_HANDLER_REGISTRY` in `mini_ork/cli/execute_handlers.py` only grow by editing
  that file, even though public `register_*` APIs already exist.
- Build a new module `mini_ork/cli/recipe_register.py` exposing
  `load_recipe_register(recipe_dir: Path) -> bool`:
  - If `<recipe_dir>/register.py` does not exist: return False (no-op, zero log noise).
  - If it exists: execute it as a module (e.g. `importlib.util.spec_from_file_location`)
    exactly once per resolved path per process (module-level `set` guard keyed on
    `Path.resolve()`; second call returns True without re-executing).
  - If executing it raises ANY exception (SyntaxError, ImportError, runtime error):
    propagate a loud failure — raise `RecipeRegisterError` with the recipe dir and the
    original traceback chained. Never swallow. A broken register.py must fail the run
    with rc != 0, not silently skip registration.
- Wire ONE call site in `mini_ork/cli/execute.py` inside `main()`, immediately after the
  recipe name is resolved from the run context (`recipe = ctx.recipe`, around line 692):
  resolve the recipe directory the same way the workflow YAML for that recipe is located
  (reuse the existing recipe-path resolution already used to load `workflow.yaml` — do
  not invent a second resolution scheme), then call `load_recipe_register(recipe_dir)`.
  If the recipe directory cannot be resolved, skip the loader (no new failure mode for
  recipes that load workflows from non-standard locations).
- The executed `register.py` is expected to call existing public APIs itself
  (`mini_ork.cli.execute.register_node_handler`,
  `mini_ork.cli.execute_handlers.register_implementer_submode`, etc.). The loader does
  NOT call any function inside the module; import side-effects are the contract.
- Size: **S** (one new module ~60 lines, one call site, one test file).

## Files in scope

- `mini_ork/cli/recipe_register.py` (new)
- `mini_ork/cli/execute.py` (single call site in `main()` near the `recipe = ctx.recipe`
  resolution; touch nothing else in this file)
- `tests/unit/test_recipe_register.py` (new)

## Definition of Done

- `load_recipe_register` behavior, each proven by a unit test in
  `tests/unit/test_recipe_register.py` using `tmp_path` recipe dirs:
  1. absent `register.py` → returns False, no exception;
  2. present `register.py` whose body mutates a sentinel (e.g. appends to a list
     injected via an importable stub module) → sentinel mutated exactly once;
  3. calling `load_recipe_register` twice on the same dir → module body executes only
     once (idempotency guard);
  4. `register.py` with a SyntaxError → `RecipeRegisterError` raised (loud failure);
  5. `register.py` raising at import time → `RecipeRegisterError` raised with original
     exception chained (`__cause__`).
- The `execute.py` call site is covered by an existing-suite regression check: the
  execute unit tests still pass unchanged.
- No behavior change for any recipe that lacks `register.py` (all current recipes lack
  one, so the default path is pure no-op).

## Verification commands

- `python3 -m pytest tests/unit/test_recipe_register.py -q`
- `python3 -m pytest tests/unit/test_mini_ork_execute_py.py -q`
- `python3 -m ruff check mini_ork/cli/recipe_register.py mini_ork/cli/execute.py --select F,E9`

## Done When

- All verification commands pass in the isolated worktree.
- Reviewer node reports pass; diff touches only the three in-scope files.
