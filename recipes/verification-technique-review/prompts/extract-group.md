Read only the declared `technique_group` artifact. It holds one technique group
from a 2026 verification-literature scan: the group definition, its catalog
techniques (each with supporting `source_ids`), and the abstract-level summaries
of every supporting paper.

Your job: distill the group into its canonical, implementable verification
techniques for mini-ork — a task OS for coding agents whose verdict is
EXECUTION-ANCHORED (classify → plan → execute → verify → reflect → improve loop;
recipe DAGs of LLM + deterministic verifier nodes; a Crucible runtime that scores
on execution status; an Assay solve-time oracle with PoC+/delta/metamorphic terms;
a three-valued behavioral verifier PROVEN/REFUTED/UNVERIFIED; a committee
aggregator; a gate registry; a promotion gate; GRASP-style frozen probe scoring;
an LLM judge that may only veto, never approve).

Write one JSON object to the requested output path, no fences:

{
  "group_id": "G01",
  "name": "copy from the input",
  "scope": "copy from the input",
  "techniques": [
    {
      "id": "G01-T01",
      "technique": "Canonical name",
      "mechanism": "3-5 sentences: how it works concretely — what it verifies, what evidence it consumes, the accept/reject/abstain rule, and what it is anchored on.",
      "reported_results": "Strongest numbers the abstracts state, each attributed with (arxiv:ID). Empty if none.",
      "evidence_strength": "strong | moderate | weak",
      "evidence": ["arxiv:2601.00001"],
      "miniork_surface": "Which mini-ork verification surface it would change (crucible runtime, assay oracle, behavioral verifier, committee, gate registry, promotion gate, probe scorer, rubric/panel scoring, abstention/oversight inbox, reward writeback, test-env sanitizer, certificate, or the eval node).",
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
- Keep negative findings: a technique that papers show FAILS or is unreliable
  (self-verification limits, judge bias, verifier blind spots, reward hacking,
  false completions) is a technique too — mark it in `mechanism` as a caution.
- Do not invent results, sources, or mini-ork features.
