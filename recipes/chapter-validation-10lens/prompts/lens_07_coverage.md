# Lens 07 — coverage

## Purpose
Every entry in assigned_source_ids must be cited somewhere in the chapter. The chapter must cover the assigned topic — title + key_topics list (if present in chapter_context) — without drifting into unrelated material.

## What you read
- `${MINI_ORK_RUN_DIR}/plan.json` — chapter_context + paths
- The chapter markdown at `chapter_artifact_path`
- Nothing else. You are ONE lens of ten; do not duplicate other lenses.

## What you check
- Each assigned_source_id appears at least once in the chapter (citation or named-paper reference)
- No source citations to ids NOT in assigned_source_ids (unless they appear in a 'See also' / 'Related work' aside)
- Title keywords appear in at least 2 paragraphs across the chapter
- No off-topic digression sections (>200 words on something unrelated to title)

## What you do NOT check
Other lenses cover those slices:
- 01 structure / 02 factuality / 03 voice_tone / 04 length_density /
  05 forbidden_constructs / 06 markdown_format / 07 coverage /
  08 coherence / 09 reader_contract / 10 synthesis_originality
Stay strictly inside your slice. If you notice an issue outside it,
DROP it — the responsible lens will catch it (or won't; either way
not your job).

## Output

Write `${MINI_ORK_RUN_DIR}/lens-07-verdict.json` (and nothing else):

```json
{
  "lens_id": "07",
  "lens_name": "coverage",
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
