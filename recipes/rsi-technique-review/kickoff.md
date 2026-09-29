# 2026 RSI technique review for the mini-ork RSI engine

Collect the top 2000 distinct 2026 papers from the LibWit arXiv corpus on
recursive self-improvement and its adjacent machinery (self-modifying agents,
verifier-gated self-edit, prompt/workflow/harness search, self-play curricula,
experience reuse and memory, RLVR/GRPO self-training, routing learning,
self-improvement limits and collapse). Extract the techniques every paper
contributes, cluster them into ten technique groups, distill each group into
implementable techniques mapped to mini-ork surfaces, and have two independent
reviewers (glm and minimax — no Anthropic lanes) score each technique's
impact, evidence, effort, and novelty against mini-ork's shipped RSI state.
Deliver a ranked implement-now / next / watch / skip decision report.

## Scope

- Files in scope: only `recipes/rsi-technique-review/**` (read-only inputs)
  and the run-local `.mini-ork/runs/<id>/` artifacts. No repository source
  file is modified by this run.
- Sources: only the 2026 LibWit/arXiv records returned by
  `recipes/rsi-technique-review/collection-plan.json`; no web results, no
  invented citations.

## Success Criteria

- `source-corpus.json` holds 2000 distinct unversioned arXiv URLs with dates,
  titles, and abstracts.
- Shard extraction covers at least 95% of the corpus
  (`technique-catalog.json` coverage block).
- `clusters.json` has exactly ten groups covering at least 95% of catalog
  techniques.
- `merged-review.json` carries scores from two reviewers.
- `rsi-technique-review.md` has Executive Summary, Implement Now, and both
  appendices; lists all ten groups; cites only corpus arXiv ids and only known
  technique ids.

## Verification Command

- `python3 recipes/rsi-technique-review/lib/rsi_review.py verify --final
  "$MINI_ORK_RUN_DIR/rsi-technique-review.md" --merged
  "$MINI_ORK_RUN_DIR/merged-review.json" --corpus
  "$MINI_ORK_RUN_DIR/source-corpus.json"` exits 0.
