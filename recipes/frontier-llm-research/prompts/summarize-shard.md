Read only the declared `source_shard` artifact. It is a bounded, audited set of
LibWit paper records, each carrying its abstract plus body-text evidence:
`evidence_mode` (`body-text` or `abstract-only`), `evidence_digest` (a short
ordered excerpt of the paper's body), and `evidence_path` (a markdown file with
the retrieved body sections, readable with the Read tool). Write exactly one JSON
object to the requested output path and no Markdown fences.

Required shape:
{
  "shard_id": "01",
  "summaries": [
    {
      "source_id": "arxiv:2601.00001",
      "rank": 1,
      "title": "...",
      "url": "https://arxiv.org/abs/2601.00001",
      "published_at": "2026-01-01",
      "retrieved_at": "2026-07-26T00:00:00Z",
      "topics": ["..."],
      "summary_paragraph": "One evidence-grounded paragraph explaining the paper's method, findings, and relevance to the requested frontier-LLM/inference/planning topic.",
      "prompt_instructions": ["A concrete instruction derived from this paper."]
    }
  ],
  "shard_techniques": [
    {
      "technique": "Short name",
      "guidance": "Deduplicated practical prompt-writing guidance from this shard.",
      "source_ids": ["arxiv:2601.00001"]
    }
  ]
}

Rules:
- Produce exactly one summary for every source in the shard; preserve its IDs,
  URLs, dates, rank, and topics exactly.
- Write one concise factual paragraph per paper from the retrieved text, not the
  abstract alone: start with the abstract and `evidence_digest`, and read
  `evidence_path` (in batches, several per Read call) for every paper whose
  method, experiments, or limits the digest does not settle. Use the body text
  for methods, numbers, and limitations; state a limitation when neither the
  abstract nor the retrieved body supports a stronger claim.
- Never claim a body-text finding for a record whose `evidence_mode` is
  `abstract-only`; that record was not retrieved.
- `prompt_instructions` is the second required paragraph in structured form:
  include 1 to 20 directly actionable instructions, each specific to the paper.
- Do not invent results, benchmark numbers, source URLs, or dates.
- `shard_techniques` must consolidate duplicated instructions within this shard
  while preserving every supporting `source_id`.
