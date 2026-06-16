# Lens 04 — length_density

## Purpose
Check word count against target_word_count (allow ±25%). Check paragraph length — neither one-sentence dribbles nor 1000-word walls. Check information density — paragraphs that say nothing (filler) vs. paragraphs cramming 5 unrelated ideas.

## What you read
- `${MINI_ORK_RUN_DIR}/plan.json` — chapter_context + paths
- The chapter markdown at `chapter_artifact_path`
- Nothing else. You are ONE lens of ten; do not duplicate other lenses.

## What you check
- Total word count within 0.75x..1.25x of target_word_count
- Paragraph length distribution: median 60-150 words; max 300 words; min 20 words (with allowed exceptions for transitions)
- Filler paragraph count == 0 (paragraphs that contain only restatement / hedging / no new claim)
- Information density: each paragraph introduces >= 1 new claim / definition / example

## What you do NOT check
Other lenses cover those slices:
- 01 structure / 02 factuality / 03 voice_tone / 04 length_density /
  05 forbidden_constructs / 06 markdown_format / 07 coverage /
  08 coherence / 09 reader_contract / 10 synthesis_originality
Stay strictly inside your slice. If you notice an issue outside it,
DROP it — the responsible lens will catch it (or won't; either way
not your job).

## Output

Write `${MINI_ORK_RUN_DIR}/lens-04-verdict.json` (and nothing else):

```json
{
  "lens_id": "04",
  "lens_name": "length_density",
  "verdict": "pass|revise|block",
  "score_0_to_10": <int>,
  "issues": [
    {
      "severity": "critical|high|med|low|info",
      "title": "<one-line problem>",
      "line_hint": "<line number or short quote>",
      "suggested_fix": "<one-line corrective action>"
    }
  ],
  "evidence_refs": ["<file:line or quote>"]
}
```

Verdict rule for this lens:
- **block**: at least one CRITICAL issue OR score < 4
- **revise**: at least one HIGH issue OR score in 4..6
- **pass**: score >= 7 AND no HIGH/CRITICAL issues
