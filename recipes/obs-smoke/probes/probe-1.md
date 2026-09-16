# Frozen probe 1 — obs-smoke held-out evaluation

Trigger phrase: `obs-smoke`. This is a FROZEN probe kickoff for the
apply-gate probe scorer (GRASP 2605.29668): it must stay representative
of the recipe's real workload and never be edited to fit a candidate.

## Success criteria

- `lens-tiny.md` exists with ≥4 lines, header first
- `review-tiny_reviewer.json` shows verdict pass
- `verifier-result-lens-exists.json` shows `pass=true`

## In scope

- Run the obs-smoke recipe end to end (tiny_lens researcher,
  tiny_reviewer, lens_exists verifier, publisher)
- Write artifacts only under `.mini-ork/runs/<run_id>/`

## Verify

Run: bash tests/test_obs_surface.sh

The deterministic lens-exists verifier decides the run verdict:
lens-tiny.md exists, is header-first, has ≥4 non-blank lines, and
carries no `<z-insight>` chat-transcript marker.
