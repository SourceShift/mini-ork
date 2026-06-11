# Code Fix Fixture 002: Clamp Bounds

## Problem

`docs/research/experiments/fixtures/code_fix/work/task_002/clamp.py` returns the
wrong bound when values fall outside the allowed range.

## Definition of Done

`clamp(value, low, high)` returns `low` when the value is below range, `high`
when above range, and the original value when already in range.

## Scope

Only `docs/research/experiments/fixtures/code_fix/work/task_002/clamp.py` may
be edited.

## Success Criteria

- `bash recipes/code-fix/verifiers/typecheck.sh` passes.
- `bash recipes/code-fix/verifiers/test.sh` passes.
- `python3 docs/research/experiments/fixtures/code_fix/work/task_002/test_clamp.py` passes.
- No other file is changed.
