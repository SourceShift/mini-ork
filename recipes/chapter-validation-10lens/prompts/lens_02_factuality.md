# Lens 02 — factuality

## Purpose
Spot-check factual claims against the assigned source_ids listed in chapter_context. Catch claims that look fabricated (no source backing) or contradict obvious source content. NOT a deep claim-by-claim audit — that's a separate research pass. Surface 3-5 highest-risk claims and judge each.

## What you read
- `${MINI_ORK_RUN_DIR}/plan.json` — chapter_context + paths
- The chapter markdown at `chapter_artifact_path`
- Nothing else. You are ONE lens of ten; do not duplicate other lenses.

## What you check
- Identify 3-5 load-bearing claims (specific dates, numerical figures, named results, causal assertions)
- For each: is the claim cited inline OR plausibly derivable from an assigned source?
- Flag any claim that name-drops a paper / dataset / metric NOT in assigned_source_ids
- Flag contradictions internal to the chapter (claim A on line X contradicts claim B on line Y)

## What you do NOT check
Other lenses cover those slices:
- 01 structure / 02 factuality / 03 voice_tone / 04 length_density /
  05 forbidden_constructs / 06 markdown_format / 07 coverage /
  08 coherence / 09 reader_contract / 10 synthesis_originality
Stay strictly inside your slice. If you notice an issue outside it,
DROP it — the responsible lens will catch it (or won't; either way
not your job).

## Output

Write `${MINI_ORK_RUN_DIR}/lens-02-verdict.json` (and nothing else):

```json
{
  "lens_id": "02",
  "lens_name": "factuality",
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
