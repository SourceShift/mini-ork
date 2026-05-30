# Kickoff: Fix off-by-one in tally accumulator

## Problem

`tally.js` line 42 uses `< arr.length` as the loop bound but the accumulator
initialises `total` from `arr[0]` before the loop, so the last element
`arr[arr.length - 1]` is never added. Tallies are always short by the last value.

## Definition of Done

`tally(arr)` returns the correct sum for every input array, including empty arrays
and single-element arrays.

## Scope

Only `src/tally.js` may be edited. No other file may be touched.

## Success Criteria

- `tally([1, 2, 3])` returns `6`.
- `tally([])` returns `0`.
- `tally([5])` returns `5`.
- All existing tests in `tests/tally.test.js` pass.
- No other file in the repository is modified.

## Model Preference

`claude-sonnet-4-5` (single-file, low complexity).

## Notes

This is the canonical minimal code-fix kickoff. Copy and adapt it for your own
single-file patches. Replace the problem statement, scope, and success criteria
with the specifics of your fix.

The `code-fix` recipe will:
1. Plan the minimal edit.
2. Apply the edit using the Edit tool.
3. Run typecheck (auto-detected) and tests.
4. Review the diff against the plan.
5. Publish (commit) on APPROVE, or open a `human_gate` on ESCALATE.
