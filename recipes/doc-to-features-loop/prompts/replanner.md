# Replanner

Apply `reflector.json` to the current `feature-index.json` and produce
`replan.json`. The goal is to make the next iteration smaller, clearer, and
more testable without losing required source-document intent.

Return strict JSON:

```json
{
  "changes": [
    {
      "type": "rerank|drop|split|add_precondition",
      "feature_id": "feature-id",
      "reason": "why this mutation follows from reflector evidence"
    }
  ],
  "next_iteration_focus": [],
  "stop": false,
  "operator_escalation": null
}
```

Replanning policy:

- Split features when the child run failed because the scope was too broad.
- Add preconditions when verifiers lacked data, fixtures, or environment setup.
- Stop only when all P0 features passed or the divergence rule fired.
- Escalate when the same failed feature set repeats without progress.
