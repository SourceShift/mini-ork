# RSI 2026 technique scan for the mini-ork RSI foundation

Collect at least 2000 distinct 2026 papers from the LibWit arXiv corpus about
recursive self-improvement (RSI) and its adjacent machinery: agents that modify
their own scaffold, prompts, tools, verifiers, memory, routing, or training
data; verifier-gated self-modification; self-play curricula; skill libraries
and experience reuse; GRPO/verifiable-reward self-training; harness and
workflow search; safe/sandboxed self-edit; self-improvement limits and model
collapse. For every paper, provide one evidence-bound summary paragraph and a
second `How to write a proper prompt` paragraph containing one to twenty
concrete instructions, each phrased so it could steer a mini-ork surface
(classify/plan/execute/verify/reflect/improve loop, goal-loop, harness factory,
lane routing, learning writeback, memory).

## Scope

- Files in scope: only `recipes/frontier-llm-research/**` and the run-local
  `.mini-ork/runs/<id>/` artifacts. The source set is only the 2026
  LibWit/arXiv paper records returned by the dated RSI collection plan
  (`collection-plan.miniork-rsi-2000.json`); no generic web results or
  invented citations.

## Success Criteria

- The minimum corpus is 2000 distinct unversioned arXiv URLs, each with a
  publication date, retrieval date, source ID, title, and abstract or metadata
  evidence.
- The required artifacts are `source-corpus.json`, ten `source-shard-*.json`
  files, ten `summary-shard-*.json` files, `technique-rollup.json`,
  `unified-techniques.md`, and `aggregation.md`.
- Techniques in `unified-techniques.md` retain their source identifiers so any
  technique can be traced back to the papers that support it.

## Verification Command

- Run `python3 recipes/frontier-llm-research/lib/research_pipeline.py
  verify --aggregation "$MINI_ORK_RUN_DIR/aggregation.md"`; it must confirm
  at least 200 source sections and one `How to write a proper prompt:` section
  for every source section.
