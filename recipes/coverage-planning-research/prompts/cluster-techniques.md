Read only the declared `technique_catalog` artifact. It lists every canonical
coverage-planning technique extracted from ~2000 papers, each with a stable `key`
(T0001…), a mechanism, and its supporting paper count.

Cluster the techniques into EXACTLY 10 technique groups. Write one JSON object to
the requested output path, no fences:

{
  "groups": [
    {
      "name": "Short group name (e.g. Plan-time completeness contracts)",
      "scope": "2-3 sentences: which mechanisms belong here and where the boundary with neighbouring groups is.",
      "technique_keys": ["T0001", "T0007"]
    }
  ]
}

Rules:
- Exactly 10 groups. Group by MECHANISM FAMILY — WHAT the mechanism guarantees
  and at which stage it acts, e.g. (adapt to what the catalog actually holds; do
  not force empty families): plan-time completeness contracts and plan
  verification; set-cover / submodular / facility-location source selection;
  hierarchical summarisation and tree reduction over large corpora; taxonomy and
  cluster induction with stable item identity; plan-then-write long-form generation
  with section-level retrieval; citation and claim grounding; deduplication,
  novelty and repetition control; context budgeting and information-loss
  measurement; coverage metrics and rubric audits; incremental re-planning and
  corpus growth.
- Every key in the catalog appears in exactly one group. Do not invent keys.
- Prefer balanced groups, but never merge unrelated mechanisms just to balance.
- Order groups by total supporting papers, largest first.
