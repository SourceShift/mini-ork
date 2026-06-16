# Lens 08 — coherence

## Purpose
Internal logical flow + transition quality + redundancy. Each section should lead naturally to the next. No paragraph should repeat what a prior paragraph said. No abrupt topic jumps without a connector sentence. Pronoun antecedents must be clear.

## What you read
- `${MINI_ORK_RUN_DIR}/plan.json` — chapter_context + paths
- The chapter markdown at `chapter_artifact_path`
- Nothing else. You are ONE lens of ten; do not duplicate other lenses.

## What you check
- Section transitions present (each H2 has a connector sentence to the prior section)
- Redundant paragraph pairs (similarity >= 0.75 by content overlap) count == 0
- Pronoun antecedent clarity — every 'it' / 'they' / 'this' has an unambiguous referent within 2 sentences
- No forward references to sections that don't exist (e.g. 'as we will see in §4' when §4 is missing)
- Logical connectors used appropriately ('however' / 'therefore' / 'thus' / 'because')

## What you do NOT check
Other lenses cover those slices:
- 01 structure / 02 factuality / 03 voice_tone / 04 length_density /
  05 forbidden_constructs / 06 markdown_format / 07 coverage /
  08 coherence / 09 reader_contract / 10 synthesis_originality
Stay strictly inside your slice. If you notice an issue outside it,
DROP it — the responsible lens will catch it (or won't; either way
not your job).

## Output

Write `${MINI_ORK_RUN_DIR}/lens-08-verdict.json` (and nothing else):

```json
{
  "lens_id": "08",
  "lens_name": "coherence",
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
