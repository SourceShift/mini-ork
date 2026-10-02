# 2026 cross-agent coordination technique review for mini-ork

Collect the top 1000 distinct 2026 papers from the LibWit arXiv corpus on
coordinating concurrent agents: detecting, preventing, and resolving overlap
and conflict when independent agents act on shared files, shared
configuration, shared state/memory, or the same task/topic. Summarise every
paper, extract the techniques it contributes, cluster them into ten technique
groups, distill each group into implementable techniques mapped to mini-ork and
ContextNest surfaces, and have three independent reviewers (glm-5.3,
MiniMax-M3, deepseek-v4-flash — no Anthropic lanes) score each technique's
impact, evidence, effort, and novelty against mini-ork's shipped coordination
state. Deliver a ranked implement-now / next / watch / skip decision report
that feeds the design of a harness-agnostic per-turn overlap protocol.

## Scope

- Files in scope: only `recipes/coord-technique-review/**` (read-only inputs)
  and the run-local `.mini-ork/runs/<id>/` artifacts. No repository source
  file is modified by this run.
- Sources: only the 2026 LibWit/arXiv records returned by
  `recipes/coord-technique-review/collection-plan.json`; no web results, no
  invented citations.

## Success Criteria

- `source-corpus.json` holds 1000 distinct unversioned arXiv URLs with dates,
  titles, and abstracts.
- Shard extraction covers at least 95% of the corpus
  (`technique-catalog.json` coverage block); `paper-digest.md` renders every
  covered paper's summary.
- `clusters.json` has exactly ten groups covering at least 95% of catalog
  techniques.
- `merged-review.json` carries scores from three reviewers.
- `coord-technique-review.md` has Executive Summary, Implement Now, and both
  appendices; lists all ten groups; cites only corpus arXiv ids and only known
  technique ids.

## Verification Command

- `python3 recipes/rsi-technique-review/lib/rsi_review.py verify --final
  "$MINI_ORK_RUN_DIR/coord-technique-review.md" --merged
  "$MINI_ORK_RUN_DIR/merged-review.json" --corpus
  "$MINI_ORK_RUN_DIR/source-corpus.json"` exits 0.
