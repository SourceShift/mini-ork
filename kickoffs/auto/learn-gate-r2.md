# Learning gate — revision 2 (Opus review of run learn-gate-20261007121336)

WIP commit 5aa97148 holds revision 1: the approval gate requires an authored lesson, and gradient
extractor failures name the lane and the provider error. Opus returned `needs_revision` with the
three findings below. Fix ONLY these. Keep everything else in the WIP commit as it is.

## Files in scope

- `mini_ork/learning/reflection_pipeline.py`: ONLY `reflection_verify_patterns`
- `mini_ork/learning/gradient_extractor.py`: ONLY `_default_dispatch`, the module-level error
  state, and the failure branch in `extract`
- `tests/unit/test_reflection_pipeline_py.py`, `tests/unit/test_gradient_extractor_py.py`

Do NOT modify any other file.

## Fixes (exact)

1. **Blocking: restore the cold-safe guard** (`reflection_pipeline.py` ~:939,
   `rows = con.execute(base_select).fetchall()`). Revision 1 removed the `sqlite3.OperationalError`
   guard, so a database without an `emergent_patterns` table now raises instead of printing `0`
   and returning `0`. Restore exactly the old contract: missing table → `print(0)`, `return 0`.
   Missing `lesson_text` column → treat every row as having no lesson (stays proposed unless
   `MO_EMERGENT_VERIFY_REQUIRE_LESSON=0`), with no exception. Guard EVERY `con.execute` in the
   function the same way. Tests: a DB with no `emergent_patterns` table → returns 0, prints `0`,
   no exception. A table without the `lesson_text` column → no exception, the row stays proposed.
2. **Stale error** (`gradient_extractor.py` ~:28). `_last_dispatch_error` is never reset, so a
   later failure can report an earlier call's error. Reset it (under the lock) at the start of
   every `_default_dispatch` call. Test: a failed call with stderr `first 400`, then a failed call
   with empty stderr → the second `extract` failure message does NOT contain `first 400`.
3. **Wrong comment** (`gradient_extractor.py` ~:27). The comment claims no read-side lock is
   needed and names a symbol that doesn't exist (`_default_d`). Make the comment match the code:
   reads and writes both take the lock.

## Verification command

The command that proves this run succeeded:

```bash
python3.11 -m pytest -q -p no:asyncio tests/unit/test_reflection_pipeline_py.py tests/unit/test_gradient_extractor_py.py tests/unit/test_context_assembler_py.py   # must exit 0
```

## Done when

- The verification command → 0 failed. Paste the summary line.
- `uvx ruff check mini_ork/learning/reflection_pipeline.py mini_ork/learning/gradient_extractor.py tests/unit/test_reflection_pipeline_py.py tests/unit/test_gradient_extractor_py.py` → clean.
- `git diff 5aa97148 --stat` touches only the files in scope.
