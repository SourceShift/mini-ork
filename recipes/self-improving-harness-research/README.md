# self-improving-harness-research

Literature → clustered technique set → book outline, for the topic of
**self-improving agent harnesses** (agents that refine their own prompts, tools,
skills, memory, sub-agents, verifiers, or routing, or adapt online). Anchored on
arXiv:2605.09998 (*Continual Harness: Online Adaptation for Self-Improving
Foundation Agents*).

```
collect (>=2000 2026 papers) → shard x10 → extract_shard x10 (glm/minimax/deepseek rotating)
  → technique_catalog (deterministic: coverage >=95%, corpus-checked ids)
  → technique_clusterer (glm: exactly 10 mechanism families)
  → cluster_splitter (deterministic: one bounded file per family)
  → extract_group x10 (rotating lanes: implementable, practitioner-framed techniques)
  → technique_compendium (deterministic: clustered, provenance-bound technique set)
  → book_spec_builder (deterministic: product-agnostic book outline, 1 chapter per family)
  → research_completeness (deterministic: ten clusters, no invented citations)
```

The only model stages are the ten shard extractors, the single clusterer, and
the ten group extractors; everything else is a deterministic verifier node, so
the run is cost-governable and replayable. Collection + sharding reuse
`../frontier-llm-research/lib/research_pipeline.py`; the catalog and cluster-split
reuse `../rsi-technique-review/lib/rsi_review.py`. The compendium + book-outline
tail is this recipe's own `lib/harness_research.py`, which replaces
rsi-technique-review's mini-ork impact-review panel.

## Outputs (under `.mini-ork/runs/<id>/`)

- `source-corpus.json` — the >=2000-paper 2026 manifest.
- `technique-catalog.json` / `clusters.json` — deduped techniques and their ten families.
- `techniques.json` — the structured clustered technique set (feeds the outline).
- `technique-compendium.md` — the human-readable **full technique set, clustered**, each technique citing only corpus arXiv ids.
- `book-outline.json` — product-agnostic book outline (one chapter per family + intro + synthesis) with a `custom_requirements` block.
- `book-brief.md` — human-readable chapter plan with key papers per chapter.

`book-outline.json` is intentionally generic: map it onto whatever book-authoring
platform you use (e.g. a `generate-plan` payload). It carries no product-specific
API shape, so the recipe stays reusable.

## Required environment

The recipe never stores the LibWit bearer token in a recipe, prompt, or run
artifact. Provide it in the invoking shell (or point `LIBWIT_MCP_JSON` at an
arxiv-libwit MCP config and `launch.sh` will lift it):

```bash
export ARXIV_API_TOKEN='...'
export MINI_ORK_LIBWIT_API_BASE='https://arxiv.libwit.ai/api'   # default
```

The book framing lives in `book.json` (topic, audience, publisher style); edit it
to retarget the book without touching the pipeline.

## Run

```bash
bin/mini-ork validate --recipe self-improving-harness-research
MINI_ORK_DRY_RUN=1 bin/mini-ork run self-improving-harness-research \
  recipes/self-improving-harness-research/kickoff.md          # graph-only, no network/model calls

# Cheap end-to-end smoke (20-paper corpus) before the full run:
MINI_ORK_COLLECTION_PLAN=$PWD/recipes/self-improving-harness-research/collection-plan.smoke.json \
MINI_ORK_RESEARCH_MIN_SOURCES=20 \
  bash recipes/self-improving-harness-research/launch.sh harness-smoke

# Full 2000-paper run (budget-capped, glm/minimax/deepseek only):
bash recipes/self-improving-harness-research/launch.sh
```

Recover a stalled run from a node: `MO_RECOVER_FROM=<node> bash
recipes/self-improving-harness-research/launch.sh <existing-run-id>`.
