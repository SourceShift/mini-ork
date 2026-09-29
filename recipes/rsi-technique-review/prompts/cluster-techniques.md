Read only the declared `technique_catalog` artifact. It lists every canonical
technique extracted from ~2000 2026 papers on recursive self-improvement, each
with a stable `key` (T0001…), a mechanism, and its supporting paper count.

Cluster the techniques into EXACTLY 10 technique groups. Write one JSON object
to the requested output path, no fences:

{
  "groups": [
    {
      "name": "Short group name (e.g. Verifier & reward construction)",
      "scope": "2-3 sentences: which mechanisms belong here and where the boundary with neighbouring groups is.",
      "technique_keys": ["T0001", "T0007"]
    }
  ]
}

Rules:
- Exactly 10 groups. Group by MECHANISM FAMILY — what the technique changes in
  an agent system (e.g. its verifier/reward, its prompts/instructions, its
  workflow/harness topology, its memory/experience store, its routing/model
  selection, its training data/weights, its curriculum/task generation, its
  safety/containment of self-edits, its evaluation/measurement of improvement,
  its multi-agent coordination) — adapt the partition to what the catalog
  actually contains; do not force empty families.
- Every key in the catalog appears in exactly one group. Do not invent keys.
- Prefer balanced groups, but never merge unrelated mechanisms just to balance.
- Order groups by total supporting papers, largest first.
