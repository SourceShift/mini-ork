# Frozen probe 1 — framework-edit single-file fix (ledger)

FROZEN probe kickoff for the apply-gate probe scorer (GRASP 2605.29668).
It grades a candidate framework-edit directive by running the `framework-edit`
recipe twice — once as committed, once with the directive appended — over this
held-out task, and comparing publish rates. Never edit it to fit a candidate:
the entire signal is that it is fixed, so an edit here silently invalidates
every prior score.

This is the easy gate arm: one file, one behavioural bug, an obvious fix.

## Problem

`ledger.py` defines `balance(entries)`, which sums `(kind, amount)` tuples for
the two kinds `"credit"` and `"refund"`. The bug: refunds are ADDED instead of
subtracted, so a refund increases the balance:

    balance([("credit", 10), ("refund", 3)])   ->   13   (expected 7)
    balance([("refund", 5)])                   ->    5   (expected -5)

Unknown kinds already raise `ValueError` and must keep doing so.

## Files in scope

- `ledger.py` — the only file that may be edited.
- `test_ledger.py` — read-only; it is the frozen test set, not a file to change.

## Verification commands

- `pytest -q test_ledger.py` — run from the fixture directory.

## Rules

Edit files directly; do not emit unified diffs.
