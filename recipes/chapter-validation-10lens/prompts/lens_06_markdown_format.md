# Lens 06 — markdown_format

## Purpose
Math wrapping, code blocks, list shape, image refs. LaTeX math must be wrapped in proper markers (e.g. <math>...</math> or display blocks). Code blocks must have a language tag. Bullet lists must be parallel-shaped (all items the same shape). Image refs must point to existing files OR have alt text.

## What you read
- `${MINI_ORK_RUN_DIR}/plan.json` — chapter_context + paths
- The chapter markdown at `chapter_artifact_path`
- Nothing else. You are ONE lens of ten; do not duplicate other lenses.

## What you check
- Bare LaTeX in prose count == 0 (every $...$ or _{ij} construct must be inside a math marker)
- Code block fenced + language-tagged
- Bullet list parallelism: items inside one list start with the same part of speech
- Image refs ![](path) — path is non-empty AND alt text is non-empty
- Table rows are properly closed (| ... | ... |); no stray pipes mid-cell

## What you do NOT check
Other lenses cover those slices:
- 01 structure / 02 factuality / 03 voice_tone / 04 length_density /
  05 forbidden_constructs / 06 markdown_format / 07 coverage /
  08 coherence / 09 reader_contract / 10 synthesis_originality
Stay strictly inside your slice. If you notice an issue outside it,
DROP it — the responsible lens will catch it (or won't; either way
not your job).

## Output

Write `${MINI_ORK_RUN_DIR}/lens-06-verdict.json` (and nothing else):

```json
{
  "lens_id": "06",
  "lens_name": "markdown_format",
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
