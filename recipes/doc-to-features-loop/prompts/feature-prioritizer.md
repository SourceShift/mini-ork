# Feature Prioritizer

Refine `feature-index.json` into an execution backlog. Keep the same feature
IDs, then normalize priority, dependency edges, and dispatch order.

Return strict JSON:

```json
{
  "features": [
    {
      "id": "stable-kebab-id",
      "priority": "P0|P1|P2",
      "depends_on": [],
      "blocks": [],
      "dispatch_wave": 1,
      "ready_reason": "why this can be sent to recursive-validate-impl now"
    }
  ]
}
```

Prioritization policy:

- P0 features must be independently dispatchable or have all prerequisites in
  an earlier wave.
- Dependency edges must point to feature IDs, not prose.
- Prefer smaller, testable features over broad epics.
- Keep blocked features in the index with a clear precondition instead of
  silently dropping them.
