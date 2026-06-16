# Lens 01 — structure

## Purpose
Validate the chapter's structural skeleton: H1/H2/H3 hierarchy is consistent; sections appear in the order declared in chapter_context (if a TOC exists); no orphan H3 without a parent H2; no duplicate H2 titles; required sections present (e.g. introduction + at least 2 body sections + a closing/synthesis when the genre demands it).

## What you read
- `${MINI_ORK_RUN_DIR}/plan.json` — chapter_context + paths
- The chapter markdown at `chapter_artifact_path`
- Nothing else. You are ONE lens of ten; do not duplicate other lenses.

## What you check
- H1 exists and matches chapter title (allowing minor punctuation drift)
- Heading depth never skips a level
- Each H2 has at least one paragraph below it (no empty sections)
- Body sections >= 2; closing section present when genre in (textbook, practical, academic)
- No duplicate H2 titles within the chapter

## What you do NOT check
Other lenses cover those slices:
- 01 structure / 02 factuality / 03 voice_tone / 04 length_density /
  05 forbidden_constructs / 06 markdown_format / 07 coverage /
  08 coherence / 09 reader_contract / 10 synthesis_originality
Stay strictly inside your slice. If you notice an issue outside it,
DROP it — the responsible lens will catch it (or won't; either way
not your job).

## Output

Write `${MINI_ORK_RUN_DIR}/lens-01-verdict.json` (and nothing else):

```json
{
  "lens_id": "01",
  "lens_name": "structure",
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
