# Frozen probe 2 — code-fix cross-module fix (interval merge)

FROZEN probe kickoff for the apply-gate probe scorer (GRASP 2605.29668).
It grades a candidate directive by running the `code-fix` recipe twice — once
as committed, once with the directive appended — over this held-out task, and
comparing publish rates. Never edit it to fit a candidate: the entire signal
is that it is fixed, so an edit here silently invalidates every prior score.

This is the recipe's hard arm: the failure surfaces in two modules, but only
one of them holds the defect.

## Problem

`intervals.py` merges overlapping `[start, end]` intervals. When an interval
is fully nested inside the one before it, the merged span is truncated to the
inner interval's end instead of keeping the outer end:

    merge([(1, 10), (2, 3)])   ->   [(1, 3)]      (expected [(1, 10)])

`schedule.py` builds on this primitive, so its queries inherit the
truncation — but the defect is in `intervals.py`, not in the caller. Fixing
the caller would leave `test_intervals.py` red.

## Files in scope

- `intervals.py` — the module that holds the defect.
- `schedule.py` — read-only unless a change there is independently required;
  it is a consumer of the primitive above.
- `test_intervals.py`, `test_schedule.py` — read-only; they are the frozen
  test set, not files to change.

## Definition of Done

`merge` returns the correct minimal cover for disjoint, overlapping, touching
and nested intervals; `contains`, `busy_minutes` and `free_slots` agree with
it; and every test in `test_intervals.py` and `test_schedule.py` passes.

## Success criteria

- `merge([(1, 10), (2, 3)]) == [(1, 10)]`
- `merge([(1, 4), (3, 6)]) == [(1, 6)]`
- `merge([(4, 6), (1, 3)]) == [(1, 3), (4, 6)]`
- `busy_minutes([(1, 10), (2, 3)]) == 9`
- `free_slots([(1, 10), (2, 3)], (0, 12)) == [(0, 1), (10, 12)]`
- No file other than `intervals.py` is modified.

## Verify

    python3 -m pytest test_intervals.py test_schedule.py
