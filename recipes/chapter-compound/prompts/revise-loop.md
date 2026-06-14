# Revise loop — apply focused patches up to N iterations, re-critique, exit on pass

You run the post-selection revise loop. Read the selected draft + its critique, apply patches via Opus, re-critique with a single Opus reviewer, exit when `passed === true` OR iteration cap reached.

## Inputs

- `${MINI_ORK_RUN_DIR}/winning-draft.md` — the selected draft (copied by selector).
- `${MINI_ORK_RUN_DIR}/critiques/critique-<selected_lens>.json` — its critique (selected_lens read from selection.json).
- `${MINI_ORK_RUN_DIR}/selection.json` — selector output (carries `selected_lens`).
- `${MINI_ORK_RUN_DIR}/chapter-compound-inputs/context.json` — carries `max_revise_iterations` (default 2 if absent).

## Loop algorithm

```
current_markdown   = read(winning-draft.md)
driving_critique   = read(critiques/critique-<selected_lens>.json)
iteration          = 1
revisions          = []

while iteration <= max_revise_iterations:
  # ── Revise pass (Opus, PATCH-ONLY) ──
  # Apply fragment_suggestions to the markdown — DO NOT rewrite prose
  # outside the named patches. Use the libwit chapter-revise prompt
  # conventions (verify-first sentence on iter ≥ 2):
  if iteration >= 2:
    prefix = "Before making any changes, first verify whether your previous answer is correct. "
  else:
    prefix = ""
  revised_markdown = opus_revise(
    current_markdown,
    driving_critique.fragment_suggestions,
    prefix + "PATCH-ONLY: apply only the named patches; do not rewrite surrounding prose."
  )

  # ── Re-critique pass (single Opus) ──
  post_critique = opus_critique(revised_markdown, context)

  # Record this iteration
  revisions.append({
    "iteration": iteration,
    "before_markdown": current_markdown,
    "after_markdown": revised_markdown,
    "driving_critique": driving_critique,
    "post_critique": post_critique,
    "passed": post_critique.overall_verdict == "ACCEPT" || mean(post_critique.axes[*].score) >= 7.5,
    "duration_ms": ...,
    "cost_usd": ...
  })

  current_markdown = revised_markdown

  if revisions[-1].passed:
    break

  # Next iteration uses the post_critique as the new driver
  driving_critique = post_critique
  iteration += 1
```

## Your output

Write `${MINI_ORK_RUN_DIR}/revisions.json`:

```json
[
  {
    "iteration": 1,
    "before_markdown": "<full markdown string>",
    "after_markdown": "<full markdown string>",
    "driving_critique": { /* full ChapterReviewJson */ },
    "post_critique":    { /* full ChapterReviewJson */ },
    "passed": true,
    "duration_ms": 12345,
    "cost_usd": 0.18
  }
  // ... per-iteration entries up to max_revise_iterations
]
```

And the FINAL chapter markdown to `${MINI_ORK_RUN_DIR}/final-chapter.md` — this is what the publisher embeds in `chapter-compound.json.final_markdown`.

## Zero-revisions case (loop never enters)

If `max_revise_iterations === 0` OR the selected draft's critique already has `overall_verdict === "ACCEPT"` AND `mean(axes[*].score) >= 7.5`:
- Write an empty array `[]` to revisions.json
- Copy winning-draft.md verbatim to final-chapter.md
- Set `revised: false` in the chapter-compound.json (the publisher node handles this)

## Hard constraints

- `iteration` MUST start at 1 (the schema-guard rejects iteration < 1).
- `passed` MUST be a strict boolean.
- `driving_critique` + `post_critique` MUST conform to ChapterReviewJson schema (the compound schema-guard recurses).
- DO NOT rewrite prose outside the patches in fragment_suggestions — patch-only contract.

## Cost discipline

- Each iteration ≈ 2 Opus calls (revise + critique). 2 iterations max = ~4 Opus calls = $0.10-0.40.
- Total compound cost target: $0.50-1.50 per chapter.
