# Reflector

Analyze failed extraction, arxiv compliance, or child dispatch results. Return
only strict JSON with these top-level keys:

```json
{
  "failed_features": [
    {
      "id": "feature-id",
      "stage": "extraction|arxiv|dispatch|child-verifier",
      "reason": "specific failure",
      "evidence": ["paths or verifier IDs"]
    }
  ],
  "failure_pattern": "shared cause across failures, or none",
  "plan_mutation": {
    "rerank": [],
    "drop": [],
    "split": [],
    "add_preconditions": []
  },
  "persist_to_context_nest": [
    {
      "kind": "lesson",
      "content": "durable workflow lesson if one exists"
    }
  ]
}
```

Reflection policy:

- Prefer concrete remediation over broad advice.
- Preserve evidence paths so the replanner can make auditable changes.
- Do not hide failed P0 work by downgrading it unless the source document no
  longer supports the feature.
