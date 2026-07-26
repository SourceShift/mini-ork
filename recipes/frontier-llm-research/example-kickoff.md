# Frontier LLM 2026 research synthesis

Collect at least 200 distinct current 2026 papers from the LibWit arXiv corpus
about frontier LLM architecture, inference-time token allocation and reasoning,
planning, prompting, recursive language models, memory, tool use, verification,
and efficient inference. For every paper, provide one evidence-bound summary
paragraph and a second `How to write a proper prompt` paragraph containing one
to twenty concrete instructions. Build one final Markdown aggregation: retain
every source summary, deduplicate overlapping techniques, and include every
unique technique with source identifiers.

## Scope

- Only `recipes/frontier-llm-research/**` and the run-local
  `.mini-ork/runs/<id>/` artifacts are in scope. The source set is only the 2026
  LibWit/arXiv paper records returned by the dated collection plan; no generic
  web results or invented citations.

## Success Criteria

- The minimum corpus is 200 distinct unversioned arXiv URLs, each with a publication
  date, retrieval date, source ID, title, and abstract or metadata evidence.
- The required artifacts are `source-corpus.json`, ten `source-shard-*.json` files,
  ten `summary-shard-*.json` files, `technique-rollup.json`,
  `unified-techniques.md`, and `aggregation.md`.

## Verification Command

- Run `python3 recipes/frontier-llm-research/lib/research_pipeline.py
  verify --aggregation "$MINI_ORK_RUN_DIR/aggregation.md"`; it must confirm
  at least 200 source sections and one `How to write a proper prompt:` section
  for every source section.
