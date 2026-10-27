# 2026 self-improving agent harness technique set + book outline

Collect at least 2000 distinct 2026 papers from the LibWit arXiv corpus on
**self-improving agent harnesses** and their adjacent machinery: agents that
refine their own prompts, tools, skills, memory, sub-agents, verifiers, routing,
or workflow — or adapt online within a single run — rather than retraining the
underlying model. Adjacent machinery in scope: verifier-gated self-modification,
process-reward / co-learning loops, skill libraries and experience reuse,
self-play curricula, trajectory/experience memory, GRPO/verifiable-reward
self-training, workflow/harness search, online/test-time adaptation, and the
limits of self-improvement (collapse, reward hacking). Anchored on
arXiv:2605.09998 (Continual Harness).

Extract the techniques every paper contributes, cluster them into ten mechanism
families (what each technique changes in the harness), distill each family into
implementable, framework-agnostic techniques for a practitioner, assemble one
clustered provenance-bound technique compendium, and emit a product-agnostic
book outline with one chapter per family plus an introduction and a closing
blueprint chapter.

## Scope

- Files in scope: only `recipes/self-improving-harness-research/**` (read-only
  inputs) and the run-local `.mini-ork/runs/<id>/` artifacts. No repository
  source file is modified by this run.
- Sources: only the 2026 LibWit/arXiv records returned by
  `recipes/self-improving-harness-research/collection-plan.json`; no web
  results, no invented citations.

## Success Criteria

- `source-corpus.json` holds at least 2000 (floor: `MINI_ORK_RESEARCH_MIN_SOURCES`)
  distinct unversioned arXiv URLs with dates, titles, and abstracts.
- Shard extraction covers at least 95% of the corpus
  (`technique-catalog.json` coverage block).
- `clusters.json` has exactly ten mechanism families covering at least 95% of
  catalog techniques.
- `technique-compendium.md` lists all ten families; every technique cites only
  arXiv ids present in the corpus.
- `book-outline.json` has one technique-cluster chapter per family (ten), plus an
  introduction and a synthesis chapter, and a non-empty `custom_requirements`.

## Verification Command

- Run `python3 recipes/self-improving-harness-research/lib/harness_research.py
  verify --outline "$MINI_ORK_RUN_DIR/book-outline.json"
  --compendium "$MINI_ORK_RUN_DIR/technique-compendium.md"
  --corpus "$MINI_ORK_RUN_DIR/source-corpus.json"`; it must confirm ten technique
  clusters, every cluster chapter carrying techniques, and no citation absent
  from the corpus.
