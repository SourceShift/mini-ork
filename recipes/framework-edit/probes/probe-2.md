# Frozen probe 2 — framework-edit two-file change (units + route)

FROZEN probe kickoff for the apply-gate probe scorer (GRASP 2605.29668).
It grades a candidate framework-edit directive by running the `framework-edit`
recipe twice — once as committed, once with the directive appended — over this
held-out task, and comparing publish rates. Never edit it to fit a candidate:
the entire signal is that it is fixed, so an edit here silently invalidates
every prior score.

This is the harder gate arm: the task spans two files and two clauses.

## Problem

`units.py` defines `to_meters(value, unit)` over a unit table that supports
only `"m"` and `"cm"`; an unknown unit raises `KeyError`. `route.py` defines
`total_distance(legs)`, which sums `to_meters` over `(value, unit)` legs.

Two changes are required:

1. Add `"km"` to the unit table in `units.py` (1 km = 1000 m).
2. Make `total_distance` return its sum rounded to 3 decimals, so
   `total_distance([(0.1, "m"), (0.2, "m")])` is exactly `0.3`.

An unknown unit such as `"mi"` must still raise `KeyError`.

## Files in scope

- `units.py` — may be edited to add the `"km"` unit.
- `route.py` — may be edited to round the total to 3 decimals.
- `test_route.py` — read-only; it is the frozen test set, not a file to change.

## Verification commands

- `pytest -q test_route.py` — run from the fixture directory.

## Rules

Edit files directly; do not emit unified diffs.
