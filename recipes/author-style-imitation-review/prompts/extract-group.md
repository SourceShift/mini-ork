Read only the declared `technique_group` artifact. It holds one technique group
from a literature scan on imitating an author's writing style: the group
definition, its catalog techniques (each with supporting `source_ids`), and the
abstract-level summaries of every supporting paper.

Your job: distill the group into its canonical, implementable techniques for a
practitioner who wants to build a system that reproduces a given author's
writing — given a handful of that author's texts, generate new text that reads
as theirs.

Write one JSON object to the requested output path, no fences:

{
  "group_id": "G01",
  "name": "copy from the input",
  "scope": "copy from the input",
  "techniques": [
    {
      "id": "G01-T01",
      "technique": "Canonical name",
      "mechanism": "3-5 sentences: how it works concretely — what goes in, the step that runs, and the signal that decides whether it worked.",
      "reported_results": "Strongest numbers the abstracts state, each attributed with (arxiv:ID). Empty if none.",
      "evidence_strength": "strong | moderate | weak",
      "evidence": ["arxiv:2509.14543"],
      "transferability": "What has to be true for this to work outside the paper's setting: domain, language, model family, amount of author text, compute. Name the conditions under which it is reported to break.",
      "adoption_sketch": "2-3 sentences: the smallest concrete experiment that would show whether this works for your author, and the metric that would prove it — prefer the four anchor metrics (authorship attribution, authorship verification, style matching, AI detection) plus a human preference check where the papers used one."
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
- Keep negative findings: a technique that papers show HURTS or fails (style
  flattening, degradation as demonstrations increase, memorization leakage,
  attribution-detector evasion collapsing) is a technique too — mark it in
  `mechanism` as a caution.
- `reported_results` must be a number or a claim the abstract actually states,
  never a plausible-sounding synthesis. Use the empty string when there is none.
- Do not invent results, sources, or capabilities.
