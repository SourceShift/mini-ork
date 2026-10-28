Read only the declared `source_shard` artifact: a bounded, audited set of
2023–2026 LibWit/arXiv paper records (title, abstract, metadata). You are
extracting the COVERAGE-COMPLETE PLANNING TECHNIQUES these papers contribute —
the reusable mechanisms by which a system that writes a long structured artifact
(book, survey, report) guarantees that EVERY item of a large requirement set is
planned exactly once, grounded in evidence, and not duplicated. Write exactly one
JSON object to the requested output path — no Markdown fences, no commentary.
Work through the shard in order and write the file incrementally if it is large;
every source must appear. Your LAST action must be the file write: never end your
turn with a message announcing the write — the node's output is whatever the file
holds when you stop.

Coverage planning, for this scan, includes: plan-time completeness contracts and
plan verification; agenda / question decomposition that enumerates every unit;
set-cover, submodular, facility-location and MMR selection of representative
sources; hierarchical summarisation and tree reduction (RAPTOR-style, map-reduce
synthesis, community reports) over corpora larger than any context; taxonomy and
cluster induction with stable item identity; plan-then-write long-form generation
with section-level retrieval and citation binding; cross-section deduplication and
novelty/repetition control; context-budget allocation and information-loss
measurement; coverage metrics and rubric audits that measure recall of a required
aspect set; and incremental re-planning when new documents arrive.

Required shape:
{
  "shard_id": "01",
  "papers": [
    {
      "source_id": "arxiv:2601.00001",
      "rank": 1,
      "title": "...",
      "url": "https://arxiv.org/abs/2601.00001",
      "published_at": "2026-01-01",
      "summary": "2-4 sentences: the problem, the method, and the headline result exactly as the abstract states it.",
      "coverage_relevance": "high | medium | low",
      "techniques": [
        {"name": "Short technique name", "mechanism": "One sentence: what it does and how.", "result": "Number or claim from the abstract, or empty string."}
      ]
    }
  ],
  "shard_techniques": [
    {"technique": "Canonical name", "mechanism": "One or two sentences merging the variants.", "source_ids": ["arxiv:2601.00001", "arxiv:2601.00002"]}
  ]
}

Rules:
- Exactly one `papers` entry per source in the shard; copy `source_id`, `rank`,
  `title`, `url`, `published_at` verbatim from the shard.
- 1 to 5 `techniques` per paper. A technique is a reusable PLANNING or SELECTION
  MECHANISM (e.g. "submodular source selection under a token budget",
  "hierarchical cluster summarisation with leaf-to-root rollup", "must-cover
  ledger checked against the plan", "section-level retrieval before writing"),
  not a topic ("long documents", "RAG") and not a benchmark name.
- `coverage_relevance`: high = builds, audits or measures the completeness /
  selection machinery itself; medium = a component such planning needs (a
  summariser, a retriever, an evaluator); low = tangential or coverage is only a
  side experiment.
- `result` only repeats what the abstract says. Never invent numbers, sources,
  URLs or dates. Empty string when the abstract gives no result.
- Keep NEGATIVE findings: coverage claims that fail, summaries that lose required
  aspects, dedupe that removes needed content, context budgets that silently drop
  items — these are techniques (cautions) too.
- `shard_techniques`: consolidate the per-paper techniques of THIS shard into
  15-40 canonical techniques. Merge true duplicates (same mechanism, different
  wording) and keep distinct mechanisms distinct. Every source id of every merged
  variant must be kept. Skip techniques supported only by low-relevance papers.
