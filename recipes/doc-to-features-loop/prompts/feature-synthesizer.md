# Feature Synthesizer

Merge the four extractor reports into one ranked `feature-index.json`. Deduplicate
features by user-visible capability and implementation dependency, preserving all
source evidence.

Before assigning P0 priority, consult arxiv-libwit for modern techniques that
could materially improve implementation quality, evaluation, safety, retrieval,
agent orchestration, UI generation, testing, or observability. Each P0 feature
must include a non-empty `modern_techniques_refs` array.

Return strict JSON:

```json
{
  "features": [
    {
      "id": "stable-kebab-id",
      "title": "short feature title",
      "priority": "P0|P1|P2",
      "tier": "S|MD|M|unknown",
      "effort": 1,
      "value": 1,
      "risk": 1,
      "source_evidence": [],
      "dependencies": [],
      "modern_techniques_refs": [
        {
          "source": "arxiv-libwit",
          "title": "paper or technique title",
          "why_relevant": "one sentence"
        }
      ],
      "dispatch_ready": true
    }
  ]
}
```

Ranking policy:

- P0 means required for the first coherent implementation wave.
- P1 means useful after P0 dependencies are complete.
- P2 means defer unless the source doc makes it a hard promise.
- Penalize features that lack testable outputs or have unresolved dependencies.
