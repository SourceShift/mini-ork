# Lens 03 — voice_tone

## Purpose
Validate the writing register matches the declared genre + publisher_style. Trade-press chapters should not read like dissertation prose; textbook chapters should not read like a Medium post. Catch jarring register shifts within the chapter (e.g. academic intro then casual mid-section).

## What you read
- `${MINI_ORK_RUN_DIR}/plan.json` — chapter_context + paths
- The chapter markdown at `chapter_artifact_path`
- Nothing else. You are ONE lens of ten; do not duplicate other lenses.

## What you check
- Register match to genre (technical / academic / trade / textbook / practical / literary / other)
- Publisher_style alignment if specified
- Sentence-length variance reasonable (no monotonous wall of long or short sentences)
- No second-person 'you' shifts in academic genre; no third-person omniscient in practical guides
- No code-switching between formal and casual across paragraphs

## What you do NOT check
Other lenses cover those slices:
- 01 structure / 02 factuality / 03 voice_tone / 04 length_density /
  05 forbidden_constructs / 06 markdown_format / 07 coverage /
  08 coherence / 09 reader_contract / 10 synthesis_originality
Stay strictly inside your slice. If you notice an issue outside it,
DROP it — the responsible lens will catch it (or won't; either way
not your job).

## Output

Write `${MINI_ORK_RUN_DIR}/lens-03-verdict.json` (and nothing else):

```json
{
  "lens_id": "03",
  "lens_name": "voice_tone",
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
