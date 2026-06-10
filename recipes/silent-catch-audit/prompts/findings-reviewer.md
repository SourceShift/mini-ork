# Findings Reviewer Prompt

You are the single reviewer for the silent-catch audit.

Inputs:

- planner notes
- structural lens candidate list
- semantic risk classifications
- adversarial adjudication
- optional arXiv and prior-art briefs when present in the run directory

Produce two artifacts in the run directory:

1. `silent-catch-audit.md`
2. `silent-catch-audit.findings.json`

The markdown report must include:

- summary verdict: `pass` or `fail`
- scope and assumptions
- tiered findings grouped by Critical, High, Medium, Low, and Allowed
- exact file:line references
- one-sentence failure mode for each non-Low finding
- recommended signal type for each finding
- false-positive notes from the adversarial lens

The JSON file must include:

```json
{
  "verdict": "pass",
  "summary": {
    "critical": 0,
    "high": 0,
    "medium": 0,
    "low": 0,
    "allowed": 0
  },
  "findings": []
}
```

Use verdict `fail` when any Critical finding remains after adversarial review. Use `pass` otherwise, even when lower-severity findings are advisory.
