# Audit Tier 4 + safety_events shipped 2026-06-15

## Problem

This turn shipped 5 artifacts as part of the Tier 4 ecosystem-launch
and Tier 2 Item 2 work:

- docs/RSP.md (Responsible Scaling Policy v1.0)
- kickoffs/roadmap-tier3-research-frontier.md (5 research-collab epics)
- kickoffs/roadmap-tier4-ecosystem-launch.md (5 ecosystem-launch epics)
- db/migrations/0036_safety_events.sql (append-only safety event log)
- lib/safety_events.sh (write + query API)

All 5 passed lightweight mechanical validation:
- shellcheck clean
- 9/9 self-test fixtures in lib/safety_events.sh
- citation_verifier_mechanical: 100% coverage (35/35 citations valid)
  across the 3 .md docs

The remaining medium-tier validation requires multi-lens panel review —
the heterogeneous-family scrutiny that single-lens self-validation
cannot provide. This audit fills that gap.

## Definition of Done

The recipe produces:

1. Four lens reports under `${MINI_ORK_RUN_DIR}/lens-*.md`:
   - GLM tactical findings: design errors, missing edge cases,
     contradiction-with-shipped-code
   - Kimi code-level refactors: lib/safety_events.sh code quality,
     SQL schema review
   - Codex LLM-dispatch + RSP review: RSP commitment realism,
     tripwire definition completeness, cost-pause + safety_events
     integration coherence
   - Opus architectural shape: does the safeguard taxonomy hang
     together? Does § 9 known-gaps map cleanly to Tier 2 + Tier 3?
2. A synthesis at `${MINI_ORK_RUN_DIR}/synthesis.md` ranking findings
   by severity × leverage / effort, with consensus markers for findings
   that appear in 2+ lenses.

## Scope

- Target artifacts (the 5 shipped this turn):
  - docs/RSP.md
  - kickoffs/roadmap-tier3-research-frontier.md
  - kickoffs/roadmap-tier4-ecosystem-launch.md
  - db/migrations/0036_safety_events.sql
  - lib/safety_events.sh
- Dimensions: design correctness, citation accuracy, internal
  consistency, completeness of tripwire taxonomy, schema/library
  contract soundness, alignment with existing primitives at
  docs/SAFETY.md:1 / lib/coalition_gate.sh:49 /
  lib/krippendorff_alpha_gate.sh:56 / lib/citation_verifier_mechanical.sh:61
- Depth: 4 parallel lenses + 1 synthesis = ~5-15 min wall-clock
- Budget: $15 max (MO_REFACTOR_AUDIT_BUDGET_USD ceiling)
- Output: read-only audit; lens reports + synthesis only

## Success Criteria

- All 4 lens reports exist + non-empty + cite ≥1 file:line each
- Synthesis cross-references all 4 lenses + has consensus markers
- `verifiers/lens-completeness.sh` exits with pass=true
- Total cost ≤ $15

## Success Command

`bash recipes/refactor-audit/verifiers/lens-completeness.sh
$MINI_ORK_RUN_DIR` exits with pass=true (verified via JSON
`{"verdict":"pass"}` or trailing exit 0).

## Non-goals

- Do NOT modify the audited files during the audit (read-only)
- Do NOT propose code diffs that need testing
- Do NOT audit dependencies (sqlite3, jq, claude CLI)
- Do NOT re-audit lib/coalition_gate.sh or lib/krippendorff_alpha_gate.sh
  or lib/citation_verifier_mechanical.sh — those are reference primitives,
  not in-scope for this audit
- Do NOT propose changes to docs/SAFETY.md — out of scope, separate epic

## Lineage

This is the first medium-tier dogfooded validation pass on mini-ork's
own Tier 4 deliverables, per the durable feedback rule saved
2026-06-15 ("medium validation required on epic-sized work"). The
chapter-validation-10lens recipe shipped at f2a0cca was reverted at
73509c9; this kickoff uses the surviving refactor-audit recipe with
the 4-lens panel (GLM / Kimi / Codex / Opus) as the equivalent
heterogeneous-family scrutiny.
