# Review Chapter 7 of "Building Resilient Data Pipelines"

## Problem

Chapter 7 ("Fault Tolerance Under Backpressure") needs editorial review
before the book goes to copy-edit. The chapter is a first draft and may
have structural gaps, citation holes, and pacing issues.

## Definition of Done

The recipe produces:

1. Four lens JSONs under `${MINI_ORK_RUN_DIR}/lens-*.json` covering:
   - GLM: structure/flow, clarity, audience fit
   - Kimi: style/voice, engagement/pacing, narrative coherence
   - Codex: factuality/citations, technical accuracy
   - Opus: originality/insight + meta-perspective
2. A synthesis at `${MINI_ORK_RUN_DIR}/chapter-review.json` with:
   - 9 authoritative axis scores (1-10)
   - 5-12 deduplicated fragment suggestions with consensus counts
   - overall_verdict ∈ {ACCEPT, MINOR_REVISION, MAJOR_REVISION, REJECT}
   - panel_disagreement_score in [0,1]
   - 3-5 sentence summary
3. Both verifiers pass (`schema.sh` + `panel-completeness.sh`)
4. Total cost ≤ $5

## Scope

- Target chapter: `chapter-review-inputs/chapter.md` (~4,200 words)
- Context: `chapter-review-inputs/context.json` (book outline, prior chapter summaries)
- Style blueprint: `chapter-review-inputs/style_blueprint.md` (O'Reilly MEAP style guide)
- Audience: senior data engineers, comfortable with Kafka and Flink
- Depth: 4 parallel lenses + 1 synthesis = ~5-8 min wall-clock
- Budget: $5 max
- Output: read-only review; no edits to chapter.md

## Success Criteria

- `chapter-review.json` is valid JSON with schema_version "1.0.0"
- All 9 axis keys present with integer scores 1-10
- overall_verdict is one of the 4 allowed enums
- panel_disagreement_score is float in [0,1]
- All 4 lens JSONs exist and contain their assigned non-null axes
- fragment_suggestions each cite an exact quoted fragment

## Non-goals

- Do NOT edit chapter.md — this is review-only
- Do NOT rewrite paragraphs — suggestions are directional
- Do NOT verify citations against live URLs — citation format and
  presence only, not URL resolution

## Lineage

This kickoff is the template for chapter-review runs. Copy and edit for
each new chapter.
