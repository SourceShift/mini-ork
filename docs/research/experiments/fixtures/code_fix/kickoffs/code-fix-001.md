# Code Fix Fixture 001: Tally Accumulator

## Problem

`docs/research/experiments/fixtures/code_fix/work/task_001/tally.py` skips the
last value for arrays with more than one element.

## Definition of Done

`tally(values)` returns the sum for empty, single-element, and multi-element
lists.

## Scope

Only `docs/research/experiments/fixtures/code_fix/work/task_001/tally.py` may
be edited.

## Success Criteria

- `bash recipes/code-fix/verifiers/typecheck.sh` passes.
- `bash recipes/code-fix/verifiers/test.sh` passes.
- `python3 docs/research/experiments/fixtures/code_fix/work/task_001/test_tally.py` passes.
- No other file is changed.
