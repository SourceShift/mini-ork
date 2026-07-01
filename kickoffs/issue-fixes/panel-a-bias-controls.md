# PANEL-a: panel anonymization + rank-aggregation helper (standalone, opt-in)

## Context
Grounded in `internal-docs/research/impl-analysis/05-llm-council-panel-bias.md`. mini-ork's
cross-family lens panels + arbiter (e.g. `recipes/recursive-validate-impl`, `lib/coalition_gate.sh`,
`lib/krippendorff_alpha_gate.sh`) already diversify model families, but the synthesizer/arbiter
sees lens reports with family identity visible and in fixed order → positional + identity bias
(karpathy/llm-council anonymizes; also caution from LLM-as-judge surveys). This phase adds the
bias-control MECHANICS as a standalone, tested helper — not yet wired into a recipe (that's
PANEL-b). Addresses issues I-1 (a second quorum/agreement signal) and I-6 (bias).

## Deliverables
1. `lib/panel_bias.sh` with pure, testable functions (no LLM calls — mechanics only):
   - `panel_anonymize <reports_dir> <out_dir> [seed]` — copy each `lens-<family>.md` to
     `resp-<LABEL>.md` with letter labels (A,B,C…) assigned in a **seed-shuffled** order (so label
     A is not always the same family / not always first), and write a `label_map.json`
     (`{"A":"glm","B":"kimi",…}`) kept OUT of the anonymized dir.
   - `panel_rank_aggregate <xrank_dir> <label_map.json>` — parse each reviewer's
     `FINAL RANKING:` block (letters, best-first), compute **Borda** points per label, de-anonymize
     via the map, and emit `panel-rank-aggregate.json` (`[{family, borda, mean_rank}]`, sorted).
   - `panel_permute_order <reports_dir> [seed]` — echo the report filenames in a seed-randomized
     order (for feeding the synthesizer prompt without primacy/recency bias).
2. All functions deterministic given a seed (so tests are reproducible); `bash`-only, no new deps.

## Smoke / DoD (must pass)
- `tests/unit/test_panel_bias.sh`:
  - `panel_anonymize`: produces `resp-A/B/C.md` with contents matching the originals, a
    `label_map.json` that round-trips, and — across two different seeds — the label→family mapping
    DIFFERS (proves shuffling; guard against a flaky equal-by-chance with ≥3 families).
  - `panel_rank_aggregate`: given 2-3 fixture reviewer files with `FINAL RANKING:` blocks, Borda
    scores are correct (hand-computed expected values) and the winner is de-anonymized to the right
    family.
  - `panel_permute_order`: same seed → same order; different seeds → (usually) different order.
- `bash -n lib/panel_bias.sh tests/unit/test_panel_bias.sh` clean. Existing gate tests unaffected.

## Constraints (scope guard)
- Add ONLY `lib/panel_bias.sh` + `tests/unit/test_panel_bias.sh`. Do NOT wire into any recipe,
  `lib/coalition_gate.sh`, or `lib/krippendorff_alpha_gate.sh` yet (PANEL-b). Pure mechanics,
  no LLM dispatch. Default behavior unchanged.
