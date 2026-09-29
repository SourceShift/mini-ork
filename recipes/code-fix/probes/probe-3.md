---
anchor: true
---
# Frozen probe 3 — code-fix anchor (clamp)

FROZEN ANCHOR probe for the apply-gate probe scorer. Anchor probes are scored
for every arm but NEVER enter the gate's accept decision: they measure whether
the recipe keeps a capability it already has while the loop optimizes the gate
probes. Their solve rate is written to `collapse_history.anchor`, which the
circuit breaker's collapse halt and the promotion verifier audit read. Never
edit it to fit a candidate, and never drop the `anchor: true` frontmatter.

## Problem

`clamp.py` defines `clamp(value, low, high)`, which should return `value`
limited to the closed range `[low, high]`. The upper comparison is inverted,
so values above `high` are returned unchanged instead of being capped:

    clamp(15, 0, 10)   ->   15     (expected 10)
    clamp(-3, 0, 10)   ->   0      (correct)
    clamp(4, 0, 10)    ->   4      (correct)

`clamp_all` in the same file maps `clamp` over a list and must not change.

## Files in scope

- `clamp.py` — the only file that may be edited.
- `test_clamp.py` — read-only; it is the frozen test set, not a file to change.

## Definition of Done

`clamp` returns a value inside `[low, high]` for every input, and every test in
`test_clamp.py` passes.

## Success criteria

- `clamp(15, 0, 10) == 10`
- `clamp(-3, 0, 10) == 0`
- `clamp(4, 0, 10) == 4`
- `clamp(10, 0, 10) == 10` and `clamp(0, 0, 10) == 0`
- `clamp_all([-1, 5, 99], 0, 10) == [0, 5, 10]`
- No file other than `clamp.py` is modified.

## Verify

    python3 -m pytest test_clamp.py
