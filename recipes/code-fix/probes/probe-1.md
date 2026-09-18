# Frozen probe 1 — code-fix single-file fix (tally)

FROZEN probe kickoff for the apply-gate probe scorer (GRASP 2605.29668).
It grades a candidate directive by running the `code-fix` recipe twice — once
as committed, once with the directive appended — over this held-out task, and
comparing publish rates. Never edit it to fit a candidate: the entire signal
is that it is fixed, so an edit here silently invalidates every prior score.

This is the recipe's easy arm. It should publish on the unmodified recipe; it
exists to catch a directive that breaks work the recipe already does.

## Problem

`tally.py` defines `tally(values)`, the sum of a list. The loop bound is
`range(1, len(values) - 1)`, but `total` is already initialised from
`values[0]` before the loop, so the last element is skipped:

    tally([1, 2, 3])   ->   3      (expected 6)
    tally([5])         ->   5      (single element short-circuits; correct)

`running_tally` in the same file is correct and must not change.

## Files in scope

- `tally.py` — the only file that may be edited.
- `test_tally.py` — read-only; it is the frozen test set, not a file to change.

## Definition of Done

`tally` returns the true sum for every input, including empty and
single-element lists, and every test in `test_tally.py` passes.

## Success criteria

- `tally([1, 2, 3]) == 6`
- `tally([]) == 0`
- `tally([5]) == 5`
- `tally([-1, 4, -2]) == 1`
- `running_tally([1, 2, 3]) == [1, 3, 6]` (unchanged behaviour)
- No file other than `tally.py` is modified.

## Verify

    python3 -m pytest test_tally.py
