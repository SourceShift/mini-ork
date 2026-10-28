# coverage-planning-research

Literature → ranked build list for LibWit's book composer: how to guarantee that
a produced book covers **every** item of a requirement set exactly once, when the
evidence corpus behind it reaches millions of documents and the planner context is
a few tens of kilobytes.

```
collect (2000 papers, 2023–2026) → shard ×10 → extract_shard ×10 (glm/minimax alternating)
  → technique_catalog (deterministic: coverage ≥95%, corpus-checked ids)
  → technique_clusterer (glm: exactly 10 groups)
  → cluster_splitter (deterministic: one bounded file per group)
  → extract_group ×10 (glm/minimax alternating: implementable coverage techniques)
  → review_packer (deterministic: evidence check + composer coverage-state snapshot)
  → impact_reviewer_glm ∥ impact_reviewer_minimax (independent scores)
  → review_merger (deterministic: priority, conservative verdict, disagreement)
  → report_writer (glm) → final_assembler → review_completeness
```

Run: `recipes/coverage-planning-research/launch.sh [run-id]` from the repo root.
The launcher seeds a run-scoped lane config restricted to glm + minimax.

Refresh what the reviewers treat as "already shipped" by editing
`context/compose-coverage-state.md` (or pointing `MO_COVERAGE_STATE_FILE`
elsewhere). Collection and sharding reuse
`../frontier-llm-research/lib/research_pipeline.py`.

This recipe is a fork of `recipes/verification-technique-review/` with a
coverage-focused collection plan, extraction/cluster/review prompts, a composer
coverage-state snapshot, and a renamed final report. The deterministic stages
(`lib/review.py`, `verifiers/*.py`) are shared verbatim apart from the
`composer_surface` field name and the output filename.

## Reviewer ground truth

`context/compose-coverage-state.md` is hand-written and must stay accurate: it is
the only thing separating "already shipped" from "worth building". It records the
measured gap from
`researcher/docs/book_gen/research/2026-10-28-compose-130-technique-coverage-feasibility.md`
(the 130-technique coverage audit) with `file:line` anchors.

## Output

`<run-dir>/compose-coverage-research.md` — executive summary, 10 technique
groups, an implement-now list where every item names a concrete file / contract /
constant / gate, then next, disagreements, cautions, skipped.
