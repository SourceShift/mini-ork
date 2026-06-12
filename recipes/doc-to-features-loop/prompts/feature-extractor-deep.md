# Deep Feature Extractor

Read the source markdown document and extract features that are implied by the
business goal but not necessarily listed as feature bullets. Focus on hidden
platform work, compliance constraints, telemetry, operational readiness,
failure handling, migrations, security boundaries, data quality, and gotchas
that would block a real implementation.

Return strict JSON:

```json
{
  "source": "deep",
  "features": [
    {
      "id": "stable-kebab-id",
      "title": "short implied feature title",
      "why_implicit": "the source assumption that makes this necessary",
      "evidence": ["source anchors or inferred requirement"],
      "risk_if_missing": "practical failure mode",
      "dependencies": [],
      "validation_hint": "test, smoke check, or operational probe"
    }
  ]
}
```

Extraction policy:

- Prefer implementation blockers over nice-to-have expansion.
- Include prerequisites that make downstream feature dispatch testable.
- Surface domain compliance and operational gaps even when the source doc uses
  business language instead of engineering terms.
- Mark uncertainty clearly; do not convert vague ideas into false certainty.
