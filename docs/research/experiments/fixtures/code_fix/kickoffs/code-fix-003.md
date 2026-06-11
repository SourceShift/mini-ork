# Code Fix Fixture 003: Slug Separator

## Problem

`docs/research/experiments/fixtures/code_fix/work/task_003/slugify.py` uses
underscores, but the expected slug format uses hyphens.

## Definition of Done

`slugify(text)` trims whitespace, lowercases text, and joins words with hyphens.

## Scope

Only `docs/research/experiments/fixtures/code_fix/work/task_003/slugify.py` may
be edited.

## Success Criteria

- `bash recipes/code-fix/verifiers/typecheck.sh` passes.
- `bash recipes/code-fix/verifiers/test.sh` passes.
- `python3 docs/research/experiments/fixtures/code_fix/work/task_003/test_slugify.py` passes.
- No other file is changed.
