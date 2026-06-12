# Surface Feature Extractor

Read the source markdown document and extract features that are directly stated.
Focus on explicit requirements, capability lists, roadmap tables, tier labels,
user stories, acceptance criteria, named deliverables, and success metrics.

Return strict JSON:

```json
{
  "source": "surface",
  "features": [
    {
      "id": "stable-kebab-id",
      "title": "short feature title",
      "evidence": ["quoted or paraphrased source anchors"],
      "tier": "S|MD|M|unknown",
      "users": ["affected user or operator"],
      "expected_artifacts": ["files, docs, endpoints, dashboards, or tests"],
      "dependencies": []
    }
  ]
}
```

Extraction policy:

- Prefer features that are explicitly named in the document.
- Preserve source wording for feature titles when it is clear.
- Split bundled roadmap bullets into separate implementable features.
- Do not invent implicit infrastructure or compliance work; leave that for the
  deep extractor.
