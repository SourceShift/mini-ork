# `chapter-compound` — multi-pass book-chapter writer

Multi-lens writer fanout + per-draft critique matrix + deterministic selection + focused revise loop. Produces a single `chapter-compound.json` audit-bundle for one book chapter.

## When to use

- You want **stylistic diversity** in chapter drafts (4 lens families produce 4 different takes; the best wins on the C1-C9 rubric).
- You want **per-pass forensics** (audit-bundle captures every draft + every critique + selection rationale + revise trace).
- You're willing to pay **~3.7× per chapter** vs single-shot writing for the diversity + forensics premium.

## When NOT to use

- Pre-prod / cost-sensitive runs. The single-shot canonical writer + Phase 1 panel critique (from libwit's `bookOrchestratorContextFirst`) is cheaper and already multi-critic.
- Books shorter than 5 chapters. The setup cost (4 writer sandboxes) doesn't amortize.
- When the LLM key budget is constrained (10-25 sandboxes per chapter = 10-25× key load).

## Pipeline shape

```
prepare → writer-fanout (4 parallel) → critique-fanout (4 parallel)
       → selector → revise-loop (N iters) → verifiers → publisher
```

## Inputs (staged by caller into `${MINI_ORK_RUN_DIR}/chapter-compound-inputs/`)

| File | Required | Purpose |
|---|---|---|
| `chapter_contract.json` | yes | Full ChapterWritingContract (required H2 count, genre rules, forbidden constructs) |
| `evidence_pack.md` | yes | Pre-fetched evidence bundle (markdown) |
| `chapter_outline.md` | yes | Chapter plan summary + key topics |
| `context.json` | yes | chapter_title, chapter_number, book_topic, audience, language, style_blueprint_summary, prior_chapters_outline, max_revise_iterations |
| `style_blueprint.md` | no (stub if absent) | Voice/tone reference |
| `seed.md` | no | Warm-start markdown; absent = cold draft |

## Output

Single artifact at `${MINI_ORK_RUN_DIR}/chapter-compound.json` matching `libwit/shared/types/chapterCompound.ts:isChapterCompoundJson` schema-guard exactly. Shape:

```jsonc
{
  "schema_version": "1.0.0",
  "chapter_title": "...",
  "chapter_number": 1,
  "drafts": [
    { "lens": "glm|kimi|codex|opus", "markdown": "...",
      "bytes": N, "duration_ms": N, "cost_usd": N,
      "accepted_for_review": bool, "reject_reason"?: "..." }
  ],
  "critique_cells": [
    { "writer_lens": "glm|kimi|codex|opus",
      "critique": { /* full ChapterReviewJson */ },
      "duration_ms": N, "cost_usd": N }
  ],
  "selection": {
    "selected_lens": "glm|kimi|codex|opus",
    "selection_strategy": "deterministic_weighted",
    "rationale": "...",
    "candidate_scores": [
      { "lens": "...", "mean_axis_score": N,
        "panel_disagreement": N, "composite_score": N }
    ]
  },
  "revisions": [
    { "iteration": N, "before_markdown": "...", "after_markdown": "...",
      "driving_critique": { /* ChapterReviewJson */ },
      "post_critique":    { /* ChapterReviewJson */ },
      "passed": bool, "duration_ms": N, "cost_usd": N }
  ],
  "final_markdown": "...",
  "revised": bool,
  "cost_summary": {
    "total_usd": N, "writer_usd": N, "critique_usd": N,
    "selection_usd": N, "revise_usd": N, "sandbox_spawns": N
  },
  "total_duration_ms": N
}
```

## Cost model

| Pass | Sandboxes | LLM calls | Cost USD | Wall clock |
|---|---|---|---|---|
| prepare | 1 | 0 | $0 | ~5s |
| writer-fanout (4 parallel) | 4 | 4 (1 per lens) | $0.30-0.80 | 60-180s (max of 4) |
| critique-fanout (4 parallel, each = 1 chapter-review = 4 lenses internally) | 4 | 16 (4 lens × 4 drafts) | $0.10-0.30 | 60-180s (max of 4) |
| selector | 1 | 1 | <$0.01 | ~5s |
| revise-loop (≤2 iter × 2 LLM calls) | 2-3 | 2-4 | $0.10-0.40 | 30-120s |
| verifiers + publisher | 1 | 0 | $0 | ~5s |
| **Total** | **~13** | **~23-25** | **$0.50-1.50** | **3-8 min** |

## Activation in libwit

Already scaffolded in libwit at:

- `shared/types/chapterCompound.ts` — schema contract + isChapterCompoundJson guard
- `server/services/bookGeneration/chapterCompoundWriterClient.ts` — BE adapter (runChapterCompoundWriter)
- `server/services/bookGeneration/bookOrchestratorContextFirst.ts` — flag-gated dispatch at C.4 critique-loop entry
- `server/resources/daytona/run-miniork-agent.cjs` — stageChapterCompoundInputs

After this recipe lands:
1. Bump `MINIORK_REPO_SHA` in `server/resources/daytona/build-snapshots.ts`
2. Rebuild snapshot: `npx tsx server/resources/daytona/build-snapshots.ts --miniork-only`
3. Flip flag: `kubectl set env -n researcher deploy/libwit-worker-book-generation FEATURE_CHAPTER_COMPOUND_WRITER=true`
4. Smoke: `kubectl exec -i -n researcher deploy/libwit-backend -- node - < server/scripts/debug-miniork-chapter-review-smoke.cjs` (probe was authored for chapter-review; adapt for compound by changing recipe name)

## Composes with

- `recipes/chapter-review/` — invoked per-draft inside critique-fanout nodes; output reused verbatim
- `libwit/docs/book_gen/handoffs/20260614-2200-upstream-miniork-chapter-compound-recipe-kickoff.md` — full architectural spec
- `libwit/docs/book_gen/ideas/20260614-2150-mini-ork-chapter-compound-writer-port.md` — why-this-recipe-exists rationale
