Read only the declared `technique_catalog` artifact. It lists every canonical
technique extracted from ~2000 2026 papers on self-improving agent harnesses,
each with a stable `key` (T0001...), a mechanism, and its supporting paper count.

Cluster the techniques into EXACTLY 10 mechanism families. Write one JSON object
to the requested output path, no fences:

{
  "groups": [
    {
      "name": "Short family name (e.g. Memory & experience reuse)",
      "scope": "2-3 sentences: which mechanisms belong here and where the boundary with neighbouring families is.",
      "technique_keys": ["T0001", "T0007"]
    }
  ]
}

Rules:
- Exactly 10 groups. Group by what the technique changes in the agent's HARNESS —
  e.g. its prompts/instructions, its tools/tool-creation, its skill library, its
  memory/experience store, its verifier/reward construction, its routing/model
  selection, its workflow/topology (sub-agents, control flow), its online/test-
  time adaptation loop, its training-data/self-distillation, its
  evaluation/measurement of improvement (including collapse and safety limits).
  Adapt the partition to what the catalog actually contains; do not force empty
  families, and never merge unrelated mechanisms just to balance.
- Every key in the catalog appears in exactly one group. Do not invent keys.
- Prefer balanced families, but keep distinct mechanisms distinct.
- Order groups by total supporting papers, largest first. These become the
  book's chapters in order, so the ordering should read as a sensible teaching
  sequence where possible.
