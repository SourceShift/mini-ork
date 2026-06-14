# Selector — pick the winning draft via deterministic weighting

You compose the 4 critique cells into a single selection decision + selection.json artifact. The selection is DETERMINISTIC (pure function of the critique outputs); your job is to compute the scores correctly + write the selection rationale.

## Inputs

Read all 4 critique cells:
- `${MINI_ORK_RUN_DIR}/critiques/critique-glm.json`
- `${MINI_ORK_RUN_DIR}/critiques/critique-kimi.json`
- `${MINI_ORK_RUN_DIR}/critiques/critique-codex.json`
- `${MINI_ORK_RUN_DIR}/critiques/critique-opus.json`

And the matching drafts:
- `${MINI_ORK_RUN_DIR}/drafts/draft-glm.md`
- `${MINI_ORK_RUN_DIR}/drafts/draft-kimi.md`
- `${MINI_ORK_RUN_DIR}/drafts/draft-codex.md`
- `${MINI_ORK_RUN_DIR}/drafts/draft-opus.md`

## Scoring formula (DETERMINISTIC)

For each draft D with critique C(D):

```
mean_axis_score(D)     = average of C(D).axes[*].score
panel_disagreement(D)  = C(D).panel_disagreement_score
accepted_count         = number of drafts where the corresponding writer
                         had accepted_for_review == true (computed from the
                         draft-side metadata; default 1 if not tracked)

composite_score(D) = mean_axis_score(D)
                   × (1 - panel_disagreement(D))
                   × (1 + log(accepted_count))
```

Pick the draft with the highest `composite_score`. Tie-break by:
1. Higher `mean_axis_score`
2. Lower `panel_disagreement`
3. Alphabetical lens order (codex < glm < kimi < opus) — stable for reproducibility

## Your output

Write `${MINI_ORK_RUN_DIR}/selection.json`:

```json
{
  "selected_lens": "glm|kimi|codex|opus",
  "selection_strategy": "deterministic_weighted",
  "rationale": "One paragraph (≤ 200 words) explaining: (a) which lens won, (b) what its strongest axes were per the critique, (c) what the runner-up was and what tipped the decision",
  "candidate_scores": [
    { "lens": "glm",   "mean_axis_score": 0.0, "panel_disagreement": 0.0, "composite_score": 0.0 },
    { "lens": "kimi",  "mean_axis_score": 0.0, "panel_disagreement": 0.0, "composite_score": 0.0 },
    { "lens": "codex", "mean_axis_score": 0.0, "panel_disagreement": 0.0, "composite_score": 0.0 },
    { "lens": "opus",  "mean_axis_score": 0.0, "panel_disagreement": 0.0, "composite_score": 0.0 }
  ]
}
```

Then COPY the winning draft to `${MINI_ORK_RUN_DIR}/winning-draft.md` — the revise-loop node reads from there.

## Hard constraints

- `selection_strategy` MUST be exactly `"deterministic_weighted"` (the v1.0 schema-guard rejects other values).
- `candidate_scores` MUST contain entries for all 4 lenses, even those rejected.
- `selected_lens` MUST be one of the 4 lens family names exactly.
- DO NOT use LLM judgment to override the deterministic score — your job is to compute + justify, not to second-guess.

## Cost discipline

- This is a synthesis node, not a generation node. Small LLM call: read 4 small JSONs, compute scores, write rationale. Target < 1000 output tokens.
