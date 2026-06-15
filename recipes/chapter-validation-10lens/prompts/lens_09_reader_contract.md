# Lens 09 — reader_contract

## Purpose
Pedagogical scaffolding for textbook/practical/academic genres. Learning objectives or hook in the introduction? Worked example or applied case in body? Recap or takeaway in closing? For trade/literary: narrative hook, character or stakes setup, payoff.

## What you read
- `${MINI_ORK_RUN_DIR}/plan.json` — chapter_context + paths
- The chapter markdown at `chapter_artifact_path`
- Nothing else. You are ONE lens of ten; do not duplicate other lenses.

## What you check
- For technical/academic/textbook/practical genres:
  - Introduction states what the reader will learn / be able to do
  - At least 1 worked example or applied case in body sections
  - Closing section includes a 1-paragraph recap or takeaway list
- For trade/literary genres:
  - Opening hook within first 200 words (concrete image, question, scenario)
  - Stakes or payoff explicit by midpoint
  - No exposition-only sections > 600 words without an anchor scene

## What you do NOT check
Other lenses cover those slices:
- 01 structure / 02 factuality / 03 voice_tone / 04 length_density /
  05 forbidden_constructs / 06 markdown_format / 07 coverage /
  08 coherence / 09 reader_contract / 10 synthesis_originality
Stay strictly inside your slice. If you notice an issue outside it,
DROP it — the responsible lens will catch it (or won't; either way
not your job).

## Output

Write `${MINI_ORK_RUN_DIR}/lens-09-verdict.json` (and nothing else):

```json
{
  "lens_id": "09",
  "lens_name": "reader_contract",
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
