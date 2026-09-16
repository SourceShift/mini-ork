# Frozen probe 2 — obs-smoke held-out evaluation

Trigger phrase: `obs-smoke`. Second member of the frozen probe set for
the apply-gate probe scorer. Worded differently from probe-1 so the two
probes are not trivially identical prompts, but same success contract.

## Success criteria

- `lens-tiny.md` present and shape-valid
- reviewer JSON verdict parseable as pass
- verifier sidecar green

## In scope

- Execute the full obs-smoke node chain
- Artifacts land under `.mini-ork/runs/<run_id>/` only

## Verify

Run: bash tests/test_obs_surface.sh

The run's own lens-exists verifier is the oracle: file exists, header
first line, at least four non-blank lines, no `<z-insight>` marker.
