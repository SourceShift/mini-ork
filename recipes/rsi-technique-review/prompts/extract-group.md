Read only the declared `technique_group` artifact. It holds one technique group
from a 2026 RSI literature scan: the group definition, its catalog techniques
(each with supporting `source_ids`), and the abstract-level summaries of every
supporting paper.

Your job: distill the group into its canonical, implementable techniques for
mini-ork — a task OS for coding agents (classify → plan → execute → verify →
reflect → improve loop; recipe DAGs of LLM + deterministic verifier nodes;
heterogeneous model lanes with a contextual-bandit router; GRPO-style advantage
writeback; reflection → textual gradients → gated apply loop; goal-loops that
treat their own runs as the improvement substrate; semantic memory; promotion
gate with non-regression checks).

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
      "reported_results": "Strongest numbers the abstracts state, each attributed with (arxiv:ID). Empty if none.",
      "evidence_strength": "strong | moderate | weak",
      "evidence": ["arxiv:2601.00001"],
      "miniork_surface": "Which mini-ork surface it would change (router, verifier/gates, reflection, apply loop, promotion gate, memory, goal-loop, recipe/harness, learning writeback, observability).",
      "adoption_sketch": "2-3 sentences: the smallest concrete change that would test it in mini-ork, and the measurement that would show it worked."
    }
  ]
}

Rules:
- 5 to 15 techniques. Merge catalog entries that are the same mechanism; split
  entries that hide two mechanisms. Ids are `<group_id>-T01`, `-T02`, … in
  order of evidence weight.
- `evidence` lists only source ids present in the input. strong = several
  independent papers with reported gains; moderate = one paper with numbers or
  several without; weak = claims without results.
- Keep negative findings: a technique that papers show HURTS or fails
  (collapse, reward hacking, zero-gain skills) is a technique too — mark it in
  `mechanism` as a caution.
- Do not invent results, sources, or mini-ork features.
