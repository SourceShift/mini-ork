# Critique — chapter-review pass on the OPUS writer's draft

You are running the **chapter-review** recipe against ONE specific writer's draft. Your inputs are different from a standalone chapter-review run because the chapter under review is the per-writer draft, not a pre-existing chapter file.

## Inputs

- `${MINI_ORK_RUN_DIR}/drafts/draft-opus.md` — the chapter draft to critique (OPUS writer's output).
- `${MINI_ORK_RUN_DIR}/chapter-compound-inputs/context.json` — reuse the context shape that chapter-review expects.
- `${MINI_ORK_RUN_DIR}/chapter-compound-inputs/style_blueprint.md` — style reference.

## Your output

Write the chapter-review JSON to:
- `${MINI_ORK_RUN_DIR}/critiques/critique-opus.json`

The JSON MUST conform exactly to the `ChapterReviewJson` schema-guard at `libwit/shared/types/chapterReview.ts:isChapterReviewJson` (same shape that the existing `chapter-review` recipe emits at `${MINI_ORK_RUN_DIR}/chapter-review.json`). Compound's `chapter-compound.json` schema-guard `isChapterCompoundJson` validates the `critique_cells[*].critique` field by recursing into `isChapterReviewJson`.

## Process

1. Stage the draft as the `chapter.md` input that chapter-review expects:
   ```bash
   mkdir -p ${MINI_ORK_RUN_DIR}/chapter-review-inputs-opus
   cp ${MINI_ORK_RUN_DIR}/drafts/draft-opus.md ${MINI_ORK_RUN_DIR}/chapter-review-inputs-opus/chapter.md
   cp ${MINI_ORK_RUN_DIR}/chapter-compound-inputs/context.json ${MINI_ORK_RUN_DIR}/chapter-review-inputs-opus/context.json
   cp ${MINI_ORK_RUN_DIR}/chapter-compound-inputs/style_blueprint.md ${MINI_ORK_RUN_DIR}/chapter-review-inputs-opus/style_blueprint.md
   ```
2. Invoke the existing chapter-review recipe via the mini-ork framework. The output is `chapter-review.json` written to a sub-run dir; copy that to `critiques/critique-opus.json`.
3. The 4 critique-fanout nodes run in parallel; each writes its own per-writer critique file.

## Critical rule

Do NOT re-implement chapter-review logic here. Recursively call the existing recipe — the whole point is that compound REUSES chapter-review's panel infrastructure verbatim, so any improvements to chapter-review automatically benefit compound.

## Cost discipline

- Each critique-fanout node = 1 chapter-review run = ~$0.10-0.30 in LLM spend.
- 4 critique-fanout nodes total = $0.40-1.20 per chapter for the critique matrix.
