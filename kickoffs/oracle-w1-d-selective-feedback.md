# Kickoff: Selective-feedback conjunction in promotion_gate.sh

## Problem

`lib/promotion_gate.sh` today checks `utility_delta > 0` and forwards to LLM-judged eval. For non-deterministic-oracle task classes (`research_synthesis`, `refactor_audit`, `blog-post`, `ui-audit`, `ops-runbook`) this is the single failure point the oracle-hardening epic exists to mitigate: the framework auto-promotes the highest-scoring panel verdict without an external grounding signal, which Zenil 2026 (arxiv:2601.05280) proves yields degenerative dynamics in the limit (entropy decay + distributional drift).

Adapala 2025 (arxiv:2509.10509) Anti-Ouroboros effect demonstrates the cheap practical remedy: a selective filter that retains only candidates passing a quality threshold BEFORE re-ingest flips collapse into emergent resilience. The selection step is doing the work of a tiny oracle slice.

## Definition of Done

For synthesis-class task classes, `lib/promotion_gate.sh` requires ALL THREE conditions to mark a candidate auto-promote-eligible:

1. Panel rubric score ≥ threshold (default 80/100)
2. CW-POR ≤ 0.3 (no authority-capture signal — sourced from `lib/cw_por.sh` if available, default-passes if the library is absent so this kickoff doesn't hard-depend on W1-C ordering)
3. ≥ 1 independent structural quality signal: citation density per lens > N (default 3), OR file-coverage delta > 0, OR finding cardinality > N (default 5)

Conjunction beats single-signal because correlated failures across the panel are less likely to also fool the second structural signal.

Deterministic-oracle task classes (`code_fix`, `db_migration`) are unchanged — still single-pass typecheck gate.

## Scope

Only `lib/promotion_gate.sh` may be edited. No other file may be touched.

## Success Criteria

- `bash -n lib/promotion_gate.sh` syntax-check clean
- Sourcing the file exposes a `mo_promote_synthesis_gate` function with the conjunction logic
- The function returns rc=0 only when all 3 conditions are met
- The function returns rc=1 with a specific failure-reason on stdout if any condition fails (`reason: low_panel_score | authority_capture | no_structural_signal`)
- Deterministic-oracle classes route through the existing single-pass typecheck path (no regression)
- Header comment cites Adapala 2025 arxiv:2509.10509 (Anti-Ouroboros selective feedback) and explains the conjunction rationale

## Model Preference

`claude-opus-4-7` — modifies the central promotion-decision file; conservative.

## Notes

Source spec in `kickoffs/oracle-hardening-v03.md` § Wave 1 — W1-D. If `lib/cw_por.sh` doesn't yet exist when this lands (parallel-dispatch ordering), the CW-POR check should default-pass (warn-only) so the gate doesn't hard-fail.
