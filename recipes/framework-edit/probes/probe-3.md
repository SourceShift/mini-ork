---
anchor: true
---
# Frozen probe 3 — framework-edit anchor (greet)

FROZEN probe kickoff for the apply-gate probe scorer (GRASP 2605.29668).
It grades a candidate framework-edit directive by running the `framework-edit`
recipe twice — once as committed, once with the directive appended — over this
held-out task, and comparing publish rates. Never edit it to fit a candidate:
the entire signal is that it is fixed, so an edit here silently invalidates
every prior score.

This is the ANCHOR arm. Its outcome is reported as `anchor_solved_frac` for
the collapse detector and is never part of the gate's before/after vectors.

## Problem

`greet.py` defines `greet(name)` returning `f"Hello, {name}"`. The task is to
make it return `f"Hello, {name}!"` — add the exclamation mark, keep the comma.

    greet("Ada")   ->   "Hello, Ada"      (expected "Hello, Ada!")

## Files in scope

- `greet.py` — the only file that may be edited.
- `test_greet.py` — read-only; it is the frozen test set, not a file to change.

## Verification commands

- `pytest -q test_greet.py` — run from the fixture directory.

## Rules

Edit files directly; do not emit unified diffs.
