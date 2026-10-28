Read only the declared `technique_group` artifact. It holds one technique group
from a 2023–2026 coverage-planning literature scan: the group definition, its
catalog techniques (each with supporting `source_ids`), and the abstract-level
summaries of every supporting paper.

Your job: distill the group into its canonical, implementable coverage techniques
for LibWit — a book composer whose pipeline is: evidence bank → plan (parts,
chapters, `mustCover`, `readerSections[].covers`, `aspect_node_ids`) → per-chapter
draft → per-section critique/revise → aspect-coverage NLI gate → cite/assurance
pipeline → commit. The composer must eventually cover a requirement ledger of
hundreds to millions of items, each exactly once, with a planner context of a few
tens of kilobytes.

Write one JSON object to the requested output path, no fences:

{
  "group_id": "G01",
  "name": "copy from the input",
  "scope": "copy from the input",
  "techniques": [
    {
      "id": "G01-T01",
      "technique": "Canonical name",
      "mechanism": "3-5 sentences: how it works concretely — what it consumes, what it guarantees, the accept/reject rule, and where in a plan→write→verify pipeline it acts.",
      "reported_results": "Strongest numbers the abstracts state, each attributed with (arxiv:ID). Empty if none.",
      "evidence_strength": "strong | moderate | weak",
      "evidence": ["arxiv:2601.00001"],
      "composer_surface": "Which composer stage it changes: evidence-bank selection, plan generation, must-cover ledger, aspect-tree construction, plan parser/gates, bank digest, section planning, writer prompt, aspect-coverage gate, dedupe/novelty pass, assurance/gap-filler, or the plan-review UI.",
      "adoption_sketch": "2-3 sentences: the smallest concrete change that would test it in LibWit, and the measurement that would show the coverage gap closed."
    }
  ]
}

Rules:
- 5 to 15 techniques. Merge catalog entries that are the same mechanism; split
  entries that hide two mechanisms. Ids are `<group_id>-T01`, `-T02`, … in order of
  evidence weight.
- `evidence` lists only source ids present in the input. strong = several
  independent papers with reported gains; moderate = one paper with numbers or
  several without; weak = claims without results.
- Keep negative findings: a mechanism that papers show FAILS, loses required
  content, or inflates cost super-linearly is a technique too — mark it in
  `mechanism` as a caution.
- Do not invent results, sources, or LibWit features. If a Paper's scope assumes a
  corpus far smaller than millions of documents, say so in `adoption_sketch`.
