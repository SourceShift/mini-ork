# verification-technique-review

Literature → ranked build list for mini-ork's verification & anchoring stack.

```
collect (2000 papers) → shard ×10 → extract_shard ×10 (glm/minimax alternating)
  → technique_catalog (deterministic: coverage ≥95%, corpus-checked ids)
  → technique_clusterer (glm: exactly 10 groups)
  → cluster_splitter (deterministic: one bounded file per group)
  → extract_group ×10 (glm/minimax alternating: implementable verification techniques)
  → review_packer (deterministic: evidence check + mini-ork verification-state snapshot)
  → impact_reviewer_glm ∥ impact_reviewer_minimax (independent scores)
  → review_merger (deterministic: priority, conservative verdict, disagreement)
  → report_writer (glm) → final_assembler → review_completeness
```

Run: `recipes/verification-technique-review/launch.sh [run-id]` from the repo root.
The launcher seeds a run-scoped lane config restricted to glm + minimax.

Refresh what the reviewers treat as "already shipped" by editing
`context/miniork-verification-state.md` (or pointing `MO_VERIFICATION_STATE_FILE`
elsewhere). Collection and sharding reuse
`../frontier-llm-research/lib/research_pipeline.py`.

This recipe is a clone of `recipes/rsi-technique-review/` with a
verification-focused collection plan, extraction/cluster/review prompts, a
verification state snapshot, and a renamed final report. The deterministic
stages (`lib/review.py`, `verifiers/*.py`) are shared verbatim apart from the
output filename.
