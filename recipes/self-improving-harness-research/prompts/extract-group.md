Read only the declared `technique_group` artifact. It holds one mechanism family
from a 2026 self-improving-agent-harness literature scan: the family definition,
its catalog techniques (each with supporting `source_ids`), and the abstract-
level summaries of every supporting paper.

Your job: distill the family into its canonical, **implementable** techniques,
written for a practitioner who is building a real LLM agent system (not for any
specific product). This becomes one chapter of a hands-on book.

Write one JSON object to the requested output path, no fences:

{
  "group_id": "G01",
  "name": "copy from the input",
  "scope": "copy from the input",
  "techniques": [
    {
      "id": "G01-T01",
      "technique": "Canonical name",
      "mechanism": "3-5 sentences: how it works concretely — inputs, the loop step, what signal decides acceptance.",
      "reported_results": "Strongest numbers the abstracts state, each attributed with (arxiv:ID). Empty string if none.",
      "evidence_strength": "strong | moderate | weak",
      "evidence": ["arxiv:2601.00001"],
      "applicability": "2-3 sentences: when a practitioner should reach for this — the conditions, agent type, or failure it fixes.",
      "implementation_sketch": "2-4 sentences: the smallest concrete way to build it into a real agent harness (data flow / pseudocode shape / which component changes), and the metric that shows it worked.",
      "cautions": "1-2 sentences: failure modes, when it hurts, or cost — e.g. collapse, reward hacking, latency, zero-gain skills. Empty string if none reported."
    }
  ]
}

Rules:
- 5 to 15 techniques. Merge catalog entries that are the same mechanism; split
  entries that hide two mechanisms. Ids are `<group_id>-T01`, `-T02`, ... in
  order of evidence weight.
- `evidence` lists only source ids present in the input. strong = several
  independent papers with reported gains; moderate = one paper with numbers or
  several without; weak = claims without results.
- Keep negative findings: a technique that papers show HURTS or fails (collapse,
  reward hacking, zero-gain skills) is a technique too — state it in `mechanism`
  and `cautions`.
- `implementation_sketch` must be concrete and framework-agnostic (no product
  names, no "add to mini-ork"); a reader on any agent stack should be able to act
  on it.
- Do not invent results, sources, numbers, or techniques absent from the input.
