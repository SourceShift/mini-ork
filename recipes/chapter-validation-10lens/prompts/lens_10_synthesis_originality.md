# Lens 10 — synthesis_originality

## Purpose
The chapter must do more than paraphrase its sources. Catch chapters that read like a literature summary with no synthesis, no novel framing, no comparative claim. Look for a distinct authorial voice / thesis / through-line that integrates the sources.

## What you read
- `${MINI_ORK_RUN_DIR}/plan.json` — chapter_context + paths
- The chapter markdown at `chapter_artifact_path`
- Nothing else. You are ONE lens of ten; do not duplicate other lenses.

## What you check
- Identifiable through-line / thesis / framing the author owns (state it in 1 sentence — if you can't, score low)
- Sources are integrated (compared / contrasted / synthesized) rather than serially summarized
- At least 1 novel claim, definition, or framing not directly attributable to a single source
- Closing synthesis section (or paragraph) ties the chapter's claims into a coherent position
- No paragraph that is a near-direct paraphrase of a single source's section (>0.8 content overlap)

## What you do NOT check
Other lenses cover those slices:
- 01 structure / 02 factuality / 03 voice_tone / 04 length_density /
  05 forbidden_constructs / 06 markdown_format / 07 coverage /
  08 coherence / 09 reader_contract / 10 synthesis_originality
Stay strictly inside your slice. If you notice an issue outside it,
DROP it — the responsible lens will catch it (or won't; either way
not your job).

## Output

Write `${MINI_ORK_RUN_DIR}/lens-10-verdict.json` (and nothing else):

```json
{
  "lens_id": "10",
  "lens_name": "synthesis_originality",
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
