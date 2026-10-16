Read only the declared `source_shard` artifact: a bounded, audited set of
LibWit/arXiv paper records (title, abstract, metadata). The theme is **imitating
an individual author's writing style** — the anchor paper is *"Catch Me If You
Can? Not Yet: LLMs Still Struggle to Imitate the Implicit Writing Styles of
Everyday Authors"* (EMNLP 2025, arxiv:2509.14543), which measures style
imitation with authorship attribution, authorship verification, style matching,
and AI detection.

You are extracting the TECHNIQUES these papers contribute to replicating a
person's writing style. Write exactly one JSON object to the requested output
path — no Markdown fences, no commentary. Work through the shard in order and
write the file incrementally if it is large; every source must appear. Your LAST
action must be the file write: never end your turn with a message announcing the
write — the node's output is whatever the file holds when you stop.

Required shape:
{
  "shard_id": "01",
  "papers": [
    {
      "source_id": "arxiv:2509.14543",
      "rank": 1,
      "title": "...",
      "url": "https://arxiv.org/abs/2509.14543",
      "published_at": "2025-09-18",
      "summary": "2-4 sentences: the problem, the method, and the headline result exactly as the abstract states it.",
      "relevance": "high | medium | low",
      "techniques": [
        {"name": "Short technique name", "mechanism": "One sentence: what it does and how.", "result": "Number or claim from the abstract, or empty string."}
      ]
    }
  ],
  "shard_techniques": [
    {"technique": "Canonical name", "mechanism": "One or two sentences merging the variants.", "source_ids": ["arxiv:2509.14543", "arxiv:2503.01234"]}
  ]
}

Rules:
- Exactly one `papers` entry per source in the shard; copy `source_id`, `rank`,
  `title`, `url`, `published_at` verbatim from the shard.
- 1 to 5 `techniques` per paper. A technique is a reusable mechanism — e.g.
  "retrieve a style reference set per topic", "descriptor-mediated style
  prompting", "style-conditioned decoding", "LoRA per author", "revise with a
  style critic" — not a topic ("personalization") and not a metric name
  ("authorship attribution").
- `relevance`: high = a mechanism for conditioning a model on an author's style,
  constructing style evidence, or measuring style fidelity; medium = a component
  such a system needs (style embedding, attribution method, style transfer);
  low = tangential.
- `result` only repeats what the abstract says. Never invent numbers, sources,
  URLs, or dates. Empty string when the abstract gives no result.
- Keep negative findings: papers reporting that a technique FAILS, does not
  transfer, or degrades with more demonstrations are among the most valuable
  entries here — record them, and say so in `mechanism`.
- `shard_techniques`: consolidate the per-paper techniques of THIS shard into
  20-45 canonical techniques. Merge true duplicates (same mechanism, different
  wording) and keep distinct mechanisms distinct. Every source id of every merged
  variant must be kept. Skip techniques supported only by low-relevance papers.
