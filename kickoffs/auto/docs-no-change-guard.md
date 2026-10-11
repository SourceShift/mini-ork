# docs recipe: fail the implementer when it changed nothing

## Goal

From Zed, a mini-ork thread whose message was just "hi" ran the `docs` recipe, edited no
file, and still ended `published` — both docs verifiers (grep-assert, link) pass
vacuously on an untouched tree. `code-fix` already fails an implementer that produced no
tree changes (`_RECIPES_REQUIRING_TREE_CHANGES` in `mini_ork/cli/execute_handlers.py`,
commit 2b880d16). The docs implementer (`doc_editor`) exists to edit documentation, so
the same guard applies.

## Files in scope

- `mini_ork/cli/execute_handlers.py` — add `"docs"` to `_RECIPES_REQUIRING_TREE_CHANGES`
  and extend its comment by one line naming why.
- `tests/unit/test_codefix_no_change_py.py` — add the docs cases next to the code-fix ones.

No other file changes.

## Tests

Following the existing code-fix cases in that file exactly (same fixtures/seams):
- docs recipe, implementer leaves the tree unchanged → node fails with `impl_no_changes`.
- docs recipe, implementer changes a file → unchanged behaviour (success).
- a recipe not in the set (e.g. `research-synthesis`) with no changes → unchanged (success).

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_codefix_no_change_py.py tests/unit/test_framework_edit_ground_truth_py.py` passes.
- `ruff check` clean on touched files; diff touches only the two files.
