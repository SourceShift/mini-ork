# Lens 05 — forbidden_constructs

## Purpose
AI-tells, purple prose, padding phrases, hallucinated meta-commentary. The chapter should not mention itself as 'this chapter explores...' more than once. No 'In conclusion, we have seen...' clichés. No 'It is important to note that...' or 'It should be emphasized that...' filler. No emoji unless the genre explicitly allows it.

## What you read
- `${MINI_ORK_RUN_DIR}/plan.json` — chapter_context + paths
- The chapter markdown at `chapter_artifact_path`
- Nothing else. You are ONE lens of ten; do not duplicate other lenses.

## What you check
- Forbidden opener count for any phrase in: ['In conclusion', 'It is important to note', 'It should be emphasized', 'As we have seen', 'In summary', 'To begin with', 'Let us now']
- 'this chapter' / 'this section' meta-references <= 2 across the whole chapter
- Emoji count == 0 (unless genre == 'trade' or 'other' AND publisher_style explicitly permits)
- Hedging adverb pile-up ('rather', 'somewhat', 'arguably', 'perhaps') < 1 per 200 words
- No exclamation marks in academic/textbook genres

## What you do NOT check
Other lenses cover those slices:
- 01 structure / 02 factuality / 03 voice_tone / 04 length_density /
  05 forbidden_constructs / 06 markdown_format / 07 coverage /
  08 coherence / 09 reader_contract / 10 synthesis_originality
Stay strictly inside your slice. If you notice an issue outside it,
DROP it — the responsible lens will catch it (or won't; either way
not your job).

## Output

Write `${MINI_ORK_RUN_DIR}/lens-05-verdict.json` (and nothing else):

```json
{
  "lens_id": "05",
  "lens_name": "forbidden_constructs",
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
