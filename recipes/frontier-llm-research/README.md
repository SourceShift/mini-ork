# Frontier LLM Research

This recipe creates one evidence-preserving research document from at least 200
LibWit/arXiv records published in 2026. It separates work into three layers:

1. Deterministic collection, ranking, and source sharding.
2. Ten model workers, each limited to its own 20-paper artifact.
3. Schema validation, technique deduplication, and deterministic Markdown assembly.

The final `aggregation.md` retains one two-part entry per source: an
evidence-bound summary paragraph followed by `How to write a proper prompt:`
with one to twenty practical instructions. `unified-techniques.md` then merges
cross-source duplicates while retaining source identifiers.

## Required environment

The recipe never stores the LibWit bearer token in a recipe, prompt, or run
artifact. Configure it in the invoking shell or an approved secret manager:

```bash
export ARXIV_API_TOKEN='...'
export MINI_ORK_LIBWIT_API_BASE='https://arxiv.libwit.ai/api'
```

`MINI_ORK_LIBWIT_API_BASE` defaults to the URL above. The collector calls the
documented batch endpoint once, fails closed if it cannot collect 200 distinct
2026 records, and does not spend model tokens on a partial corpus.

## Run

```bash
bin/mini-ork providers status --workflow recipes/frontier-llm-research/workflow.yaml
bin/mini-ork validate --recipe frontier-llm-research
bin/mini-ork run frontier-llm-research recipes/frontier-llm-research/example-kickoff.md
```

For a graph-only check that makes no network or model calls:

```bash
MINI_ORK_DRY_RUN=1 bin/mini-ork run frontier-llm-research recipes/frontier-llm-research/example-kickoff.md
```
