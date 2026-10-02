Read only the declared `source_shard` artifact: a bounded, audited set of 2026
LibWit/arXiv paper records (title, abstract, metadata). You are extracting the
TECHNIQUES these papers contribute to COORDINATING CONCURRENT AGENTS: detecting,
preventing, or resolving overlap and conflict when several independent agents
(LLM agents, coding agents, worker processes, humans) act on shared files,
shared configuration, shared memory/state, or the same task/topic. Write exactly
one JSON object to the requested output path — no Markdown fences, no
commentary. Work through the shard in order and write the file incrementally if
it is large; every source must appear. Your LAST action must be the file write:
never end your turn with a message announcing the write — the node's output is
whatever the file holds when you stop.

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
      "relevance": "high | medium | low",
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
- 1 to 5 `techniques` per paper. A technique is a reusable mechanism (e.g.
  "pre-action intent publication to a shared board", "lease-based resource
  claim with heartbeat expiry", "semantic conflict check on overlapping
  diffs"), not a topic ("coordination") and not a benchmark name.
- `relevance`: high = the paper directly detects, prevents, or resolves overlap
  or conflict between concurrent agents (concurrency control, claims/leases,
  intent sharing, shared workspaces, conflict-aware memory, merge/semantic
  conflict detection, runtime coordination monitoring, coordination failure
  diagnosis); medium = a component such a protocol needs (communication
  protocol, shared memory store, agent identity/provenance, task allocation,
  sandboxing, handoff); low = tangential (e.g. robot path planning or MARL
  reward shaping with no transferable runtime coordination mechanism).
- `result` only repeats what the abstract says. Never invent numbers, sources,
  URLs, or dates. Empty string when the abstract gives no result.
- `shard_techniques`: consolidate the per-paper techniques of THIS shard into
  15–40 canonical techniques. Merge true duplicates (same mechanism, different
  wording) and keep distinct mechanisms distinct. Every source id of every merged
  variant must be kept. Skip techniques supported only by low-relevance papers.
