# recipe: chapter-review

A heterogeneous-LLM panel review recipe for book chapters. Four model
families (GLM / Kimi / Codex / Opus) critique a chapter along 9 axes,
then an Opus synthesizer composes a single authoritative
`chapter-review.json` with scores, fragment suggestions, an overall
verdict, and a panel-disagreement metric.

## What this recipe does

Given a kickoff describing **what chapter to review** and **what
audience/style constraints apply**, the recipe:

1. **Classify** — routes to `chapter_review` task class
2. **Execute** — dispatches 4 lenses in **parallel** (each under a
different model lane):
   - **glm-lens** → structure/flow, clarity/conciseness, audience fit
   - **kimi-lens** → style/voice, engagement/pacing, narrative coherence
   - **codex-lens** → factuality/citations, technical accuracy
   - **opus-lens** → originality/insight + meta-perspective for synthesizer
3. **Synthesize** — Opus reviewer composes `chapter-review.json` with:
   - 9 authoritative axis scores (1-10)
   - Deduplicated fragment suggestions with consensus counts
   - `overall_verdict` ∈ {ACCEPT, MINOR_REVISION, MAJOR_REVISION, REJECT}
   - `panel_disagreement_score` ∈ [0,1] (mean per-axis normalized variance)
4. **Verify** — two verifier nodes run in series:
   - `schema.sh` — asserts JSON validity, all 9 axis keys, strict schema_version
   - `panel-completeness.sh` — asserts all 4 lens artifacts exist and that
the disagreement score is recomputable from raw lens scores
5. **Publish** — commits `chapter-review.json` to the repo

## Output artifacts

- `${MINI_ORK_RUN_DIR}/lens-glm.json`
- `${MINI_ORK_RUN_DIR}/lens-kimi.json`
- `${MINI_ORK_RUN_DIR}/lens-codex.json`
- `${MINI_ORK_RUN_DIR}/lens-opus.json`
- `${MINI_ORK_RUN_DIR}/chapter-review.json` — final structured review

## When to use

- Pre-publication editorial review of technical book chapters
- Pedagogical QA for course-material chapters
- Style-coherence checks across chapters in a multi-author volume
- Reviewing draft chapters before peer review submission

## When NOT to use

- For entire books — use `recipes/blog-cohesion/` (chapter-review is
  scoped to single chapters)
- For fiction requiring deep emotional critique — lenses are optimized
  for non-fiction / technical prose
- For chapters under 500 words — manual review is faster

## Cost expectation

| Scale | Estimated cost | Wall-clock |
|---|---|---|
| Single chapter, ~3K words | $1-2 | 3-5 min |
| Single chapter, ~10K words | $2-3 | 5-8 min |
| Deep technical chapter with citations | $3-5 | 8-12 min |

Cost dominated by Opus synthesis (long context). GLM/Kimi/Codex are
cheap-or-free lanes.

## How to run

```bash
# 1. Write a kickoff describing the chapter and review goals
cp ~/ps/mini-ork/recipes/chapter-review/example-kickoff.md ./my-review.md
# Edit my-review.md — point at chapter.md, set audience, depth.

# 2. Stage inputs
mkdir -p ./chapter-review-inputs/
cp my-chapter.md ./chapter-review-inputs/chapter.md
cp book-context.json ./chapter-review-inputs/context.json
# optional: cp style-guide.md ./chapter-review-inputs/style_blueprint.md

# 3. Dispatch
mini-ork run chapter-review ./my-review.md

# 4. The review lands under .mini-ork/runs/<run_id>/chapter-review.json
```

## Customization

| Knob | Where | Effect |
|---|---|---|
| Axis count | `prompts/*.md` axes tables | Add C10, C11, etc. by updating all 4 lens + synthesis prompts |
| Models per lens | `workflow.yaml` node.model_lane | Swap glm→haiku, opus→sonnet, etc. |
| Disagreement threshold | `prompts/synthesis.md` | Change the 0.4 escalation cutoff |
| Output target | `artifact_contract.yaml` outputs | Publish to docs/reviews/, GitHub Issues, etc. |

## Failure-mode coverage (verifier contracts)

| Failure mode | Verifier | How it's caught |
|---|---|---|
| Missing axis key in JSON | schema.sh | jq asserts every C1..C9 by name |
| Score out of 1-10 range | schema.sh | jq integer range check |
| Invalid overall_verdict | schema.sh | enum whitelist |
| Missing lens artifact | panel-completeness.sh | file existence + non-null axis check |
| Disagreement score not recomputable | panel-completeness.sh | recomputes variance from panel scores, asserts match |
| Lens axis ownership bleed | panel-completeness.sh | asserts non-assigned axes are null in each lens JSON |
| JSON syntax error | schema.sh | python3 json.load gate |

## See also

- `recipes/refactor-audit/` — the 4-family panel shape this recipe inherits
- `docs/EXTENSION.md` — adding new axes or lenses
- `docs/SAFETY.md` — bounded-autonomy ladder (reviews are read-only)
