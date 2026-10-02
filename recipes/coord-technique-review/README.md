# coord-technique-review

Literature → ranked build list for a cross-agent overlap protocol (detect and
arrange concurrent agents touching the same files, settings, state, or topic).

```
collect (1000 papers) → shard ×10 → extract_shard ×10 (glm/minimax/deepseek rotating)
  → technique_catalog (deterministic: coverage ≥95%, corpus-checked ids)
  → paper_digest (deterministic: per-paper summaries as Markdown)
  → technique_clusterer (glm: exactly 10 groups)
  → cluster_splitter (deterministic: one bounded file per group)
  → extract_group ×10 (rotating lanes: implementable techniques)
  → review_packer (deterministic: evidence check + coordination state snapshot)
  → impact_reviewer_{glm,minimax,deepseek} (independent scores)
  → review_merger (deterministic: priority, most-conservative verdict, disagreement)
  → report_writer (glm) → final_assembler → review_completeness
```

Run: `recipes/coord-technique-review/launch.sh [run-id]` from the repo root.
The launcher seeds a run-scoped lane config restricted to glm + minimax +
deepseek (pinned to `deepseek-v4-flash`).

The deterministic stages reuse `../rsi-technique-review/lib/rsi_review.py`;
collection and sharding reuse `../frontier-llm-research/lib/research_pipeline.py`.
Refresh what the reviewers treat as "already shipped" by editing
`context/miniork-coord-state.md` (or pointing `MO_COORD_STATE_FILE` elsewhere).
