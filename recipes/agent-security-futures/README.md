# Agent Security Futures

One evidence-preserving briefing on where AI agent security is going, built
from a 2000-paper 2026 corpus. Answers three questions in one document:

1. What are the trending topics in agent security now?
2. How does the security of agents develop over the next 12-36 months?
3. How are people affected, and how should a reader prepare?

The run separates work into three layers:

1. Deterministic collection, ranking, sharding, catalog, cluster split, score
   merging, assembly and verification (`lib/review.py`, `verifiers/*`).
2. Model workers, each reading one bounded artifact: ten shard extractors, one
   theme clusterer, ten theme analysts, two independent forecast reviewers, one
   report writer.
3. The completeness verdict, which fails closed on missing sections, unknown
   citations, unknown technique ids, or a corpus without evidence provenance.

The final `agent-security-futures.md` keeps the report body, then a ranked
appendix of every technique with its evidence ids and a per-theme appendix.
Scores come from two independent reviewer lanes from different model families;
disagreement is flagged, never averaged away.

Corpus: the collection plan carries 44 security queries plus 6 consequence
queries (governance, economics, labour, critical infrastructure, incidents,
scaling risk), 100 results each, deduplicated and ranked, and the collector
fails closed if fewer than 2000 distinct 2026 records come back.

## Required environment

```bash
export ARXIV_API_TOKEN='...'
export MINI_ORK_LIBWIT_API_BASE='https://arxiv.libwit.ai/api'
```

`launch.sh` lifts the token from a local arxiv-libwit MCP config when
`ARXIV_API_TOKEN` is unset, and never writes it into a recipe or run artifact.

## Run

```bash
bin/mini-ork validate --recipe agent-security-futures
bash recipes/agent-security-futures/launch.sh
# graph-only check, no network or model calls:
MINI_ORK_DRY_RUN=1 bin/mini-ork run agent-security-futures recipes/agent-security-futures/kickoff.md
```

The evidence discipline the report is held to:

- a paper is summarized from its abstract alone only when no body text was
  retrievable, and the corpus records which;
- reported numbers are as stated in the retrieved text, never reproduced;
- forecasts are labelled as extrapolations and carry the indicator that would
  refute them;
- every cited arXiv id must exist in the corpus and every cited technique id in
  `merged-review.json`.
