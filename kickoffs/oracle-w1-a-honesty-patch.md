# Kickoff: Positioning honesty patch — class-restricted auto-promote

## Problem

The framework's positioning document `docs/positioning/why-mini-ork.md` claims "self-improving" without explicit class-restriction. Per the 2026-06-04 self-audit synthesis Section 5 (Hardest Open Question) and the 9-paper research brief (oracle-hardening-v03.md epic), this claim has a class-restricted truth: deterministic-oracle task classes (`code_fix`, `db_migration`) can safely auto-promote because the oracle is external ground truth, but synthesis-class recipes (`research_synthesis`, `refactor_audit`, `blog-post`, `ui-audit`, `ops-runbook`) cannot — their promotion gate is LLM-judged by the same family distribution that produces candidates (Zenil 2026 entropy-decay proof + Setlur 2025 ICML 82-cite TTS-without-verification result).

## Definition of Done

`docs/positioning/why-mini-ork.md` includes a new explicit paragraph + 2-row taxonomy table stating that auto-promotion via `mini-ork promote` is restricted to deterministic-oracle task classes. Synthesis-class candidates require operator review.

## Scope

Only `docs/positioning/why-mini-ork.md` may be edited. No other file may be touched.

## Success Criteria

- `grep -c "deterministic oracle" docs/positioning/why-mini-ork.md` returns ≥ 1
- `grep -c "manual-promote-only\|operator review" docs/positioning/why-mini-ork.md` returns ≥ 1
- A markdown table appears with 2 rows: "Deterministic" → auto-promote / "LLM-judged only" → manual
- Citations to Zenil 2026 arxiv:2601.05280 + Setlur 2025 arxiv:2502.12118 + DeVilling 2025 arxiv:2510.21861 included as inline-link references
- No other file in the repository is modified

## Model Preference

`claude-sonnet-4-5` — pure documentation edit, single-file low-complexity.

## Notes

Source paragraph that drives this kickoff lives in the epic
`kickoffs/oracle-hardening-v03.md` § Wave 1 — W1-A. The downstream research brief
documenting per-paper rationale is at the host application's
`docs/_meta/research/20260605-self-evolution-oracle-arxiv-summaries.md`.
