# Kickoff: ρ hard-block gate — convert panel_topology_telemetry from observation to enforcement

## Problem

`lib/topology_metrics.sh:measure_topology` computes pairwise output correlation ρ after each panel run and writes to `panel_topology_telemetry`. Today the values are visible but the synthesizer in `bin/mini-ork-execute` runs regardless of ρ. The Rajan 2025 (arxiv:2511.16708) submodularity precondition is documentation-only.

Bertalanič 2026 (arxiv:2605.00914) demonstrates empirically that homogeneous N-agent panels are *worse* than single-agent self-correction on the same compute budget — the family-diversity precondition is load-bearing, not decorative. The framework cites it in positioning but doesn't enforce it at the dispatch level.

## Definition of Done

After `measure_topology` in the panel-execute path, if `rho >= 0.25` OR if 2+ lenses route to the same model family per `config/agents.yaml`, the synthesizer node is aborted, run verdict `COALITION_ABORT` is emitted, and the operator must either widen family diversity or accept the coalition-flagged lens reports without synthesis.

## Scope

Only these files may be edited:
- `bin/mini-ork-execute` (add the gate check between `measure_topology` and synthesizer dispatch)
- `lib/topology_metrics.sh` (add `family_count` helper if needed)

No other file may be touched. No new migrations required (the `panel_topology_telemetry` table already exists).

## Success Criteria

- `bin/mini-ork-execute --help` mentions the new `MO_RHO_THRESHOLD` and `MO_FAMILY_DIVERSITY_GATE` environment variables
- Running the existing refactor-audit recipe with synthetic correlated lens outputs (smoke fixture) produces `verdict: COALITION_ABORT` and skips the synthesizer
- Running with the canonical 4-distinct-family panel proceeds as before (no regression)
- `bash -n bin/mini-ork-execute` syntax-check clean
- `bash -n lib/topology_metrics.sh` syntax-check clean

## Model Preference

`claude-opus-4-7` — touches the canonical dispatch path; conservative architectural change.

## Notes

Source spec in `kickoffs/oracle-hardening-v03.md` § Wave 1 — W1-B.
Threshold defaults: `MO_RHO_THRESHOLD=0.25` (Rajan 2025), `MO_FAMILY_DIVERSITY_GATE=strict` (require lens count == family count).
