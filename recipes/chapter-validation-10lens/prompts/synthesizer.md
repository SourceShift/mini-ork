# Synthesizer — chapter-validation-10lens

You receive 10 lens verdicts. Combine them into ONE final verdict the
publisher will read and emit.

## What you read
- `${MINI_ORK_RUN_DIR}/plan.json` — chapter_context
- `${MINI_ORK_RUN_DIR}/lens-01-verdict.json` through `lens-10-verdict.json`
  (10 files; if any are missing, treat that lens as `block` with a
  single critical issue: "lens did not produce a verdict")

## What you produce

Write `${MINI_ORK_RUN_DIR}/panel-verdict.json`:

```json
{
  "pass": <boolean>,
  "overall_verdict": "pass|revise|block",
  "weighted_score": <number 0-100>,
  "lens_results": [
    {
      "lens_id": "01",
      "lens_name": "structure",
      "verdict": "pass|revise|block",
      "score_0_to_10": <int>,
      "critical_issue_count": <int>,
      "high_issue_count": <int>
    },
    /* … repeated for lenses 02..10 */
  ],
  "critical_issues": [
    /* concatenated critical+high issues from ALL lenses, deduplicated by title */
    {"lens_id": "05", "severity": "critical", "title": "...", "line_hint": "...", "suggested_fix": "..."}
  ],
  "recommended_action": "<one short paragraph: what the author should do next>"
}
```

## Aggregation rules (deterministic — do not improvise)

**weighted_score** = sum over 10 lenses of `score_0_to_10 * weight[lens]`,
normalized to 0-100. Default equal weights (10 each); override via
`${MINI_ORK_RUN_DIR}/plan.json::lens_weights` map if present.

**overall_verdict**:
- `pass` — every lens verdict is `pass` AND weighted_score >= 75
- `revise` — at least 1 lens `revise` AND no lens `block` AND
  weighted_score >= 50
- `block` — any lens `block` OR len(critical_issues) >= 2 OR
  weighted_score < 50

**pass** = `(overall_verdict == "pass")`.

**critical_issues** — pull all `severity ∈ (critical, high)` issues
from all 10 lens-verdict files. Dedupe by `title` (case-insensitive,
first occurrence wins). Sort by severity (critical first), then by
lens_id ascending. Cap at 20 entries — if more, surface a count in
`recommended_action`.

**recommended_action** — one short paragraph (≤ 80 words). If
overall_verdict == `pass`, say "Ready to ship" + cite the top
strength. If `revise`, name the 2-3 most important fixes. If `block`,
name the load-bearing failure + suggest the single most useful next
step.

Write the file. Nothing else.
