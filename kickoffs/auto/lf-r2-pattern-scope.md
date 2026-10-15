# LF-R2 emergent patterns are scoped to the task class before ranking

## Goal

`mini_ork/context_assembler.py` `_approved_emergent_rows` returns every approved
pattern regardless of task class; `task_class` only scopes the UCB utility
bookkeeping. 72% of pattern injections (1928 of 2670) are cross-class: the
code_fix reviewer of `run-1791930074-82118` got patterns about `__classify__`,
`__reflect__`, goal_loop and self_migrate and one code_fix pattern. All 50
approved rows carry their class in `cluster_label`
(`cluster: task_class=code_fix status=failure (freq=11 in window)`). Decision
record: `docs/plans/2026-10-15-learning-loop-fixes.md` (R2).

All paths are relative to the repository root.

## Mechanism (exact)

1. Migration `db/migrations/0075_emergent_pattern_task_class.sql` (next free
   number on `origin/main` if taken): `ALTER TABLE emergent_patterns ADD COLUMN
   task_class TEXT`, then backfill it from `cluster_label`: the token after
   `task_class=` up to the next space; rows without that token stay NULL.
2. `mini_ork/context_assembler.py`: add `pattern_task_class(cluster_label) -> str | None`
   (the same parse, in Python). `_approved_emergent_rows(dbp, task_class=None)`:
   with `task_class=None` the result is unchanged (other readers want every
   approved row); with a class, it returns that class's rows first (strength
   order), then at most `MO_PATTERN_CROSS_CLASS_MAX` (default 1) rows of other
   classes, only when the own-class rows are fewer than the caller's limit, and
   never rows whose class starts with `__` (internal classes) or is NULL. If the
   `task_class` column is missing (pre-migration DB), parse it from
   `cluster_label` instead — cold-safe.
3. Both injection paths — the static pattern block and `semantic_lessons_md`
   (UCB) — pass the run's task class, so they select from the same pool.
   The UCB memory scope key becomes `f"{task_class or 'generic'}#tc1"` so win/loss
   history from the unscoped era is not mixed with the scoped era.
4. `mini_ork/learning/reflection_pipeline.py`: the `INSERT INTO emergent_patterns`
   (around line 605) also writes `task_class` (via `pattern_task_class`), when the
   column exists.

## Files in scope

- `db/migrations/0075_emergent_pattern_task_class.sql` (new; renumber if taken)
- `mini_ork/context_assembler.py`
- `mini_ork/learning/reflection_pipeline.py`
- `tests/unit/test_pattern_task_scope.py` (new)

## Tests (`tests/unit/test_pattern_task_scope.py`, hermetic temp DB)

- a fixture of approved patterns shaped like the live labels (classes
  `code_fix`, `framework_edit`, `__classify__`, `__reflect__`, `goal_loop`,
  `self_migrate`, all with `lesson_text`): for `code_fix`, both the static block
  and `semantic_lessons_md` return only `code_fix` patterns plus at most one
  non-internal cross-class pattern, and never `__classify__`/`__reflect__`;
- `_approved_emergent_rows(dbp)` with no class returns every approved row (unchanged);
- the migration SQL applied to a table with the three live label shapes sets
  `task_class` to `recipe_authoring`, `framework_edit`, `code_fix`, and leaves a
  label without `task_class=` NULL;
- on a DB without the column, selection still scopes by parsing the label;
- a new pattern inserted by the reflection pipeline carries `task_class`.

## Verification commands

- `/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_pattern_task_scope.py tests/unit/test_context_assembler_py.py tests/unit/test_inject_verified_only.py`

## Self-application measurement

```measure
/Volumes/docker-ssd/ps/mini-ork/.venv/bin/python -m pytest -q tests/unit/test_pattern_task_scope.py --junitxml="${MINI_ORK_RUN_DIR}/lf-r2-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/lf-r2-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Never rebase, merge or reset
the worktree during the run. Do not change `_verified_theme_lessons` or the
theme selection (R1 owns it), do not touch `mini_ork/context_v2.py`,
`mini_ork/cli/execute.py`, `mini_ork/cli/plan.py`, and do not write to the live
DB under `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/`.
