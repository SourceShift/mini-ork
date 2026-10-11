Read only the declared `technique_catalog` artifact. It lists every canonical
verification technique extracted from ~2000 2026 papers, each with a stable
`key` (T0001…), a mechanism, and its supporting paper count.

Cluster the techniques into EXACTLY 10 technique groups. Write one JSON object
to the requested output path, no fences:

{
  "groups": [
    {
      "name": "Short group name (e.g. Execution-anchored and test-based verification)",
      "scope": "2-3 sentences: which mechanisms belong here and where the boundary with neighbouring groups is.",
      "technique_keys": ["T0001", "T0007"]
    }
  ]
}

Rules:
- Exactly 10 groups. Group by MECHANISM FAMILY — WHAT the verification technique
  establishes and HOW it is anchored, e.g. (adapt to what the catalog actually
  holds; do not force empty families): execution/test-based checking; oracle and
  ground-truth construction; metamorphic / property-based / invariant checking;
  formal, proof- and certificate-based verification; verifier and reward-model
  construction and AUDITING; reward-hacking and specification-gaming detection;
  LLM-judge reliability, bias and calibration; abstention, selective prediction
  and uncertainty quantification; grounding / citation / claim / trajectory
  verification; benchmark and evaluation validity.
- Every key in the catalog appears in exactly one group. Do not invent keys.
- Prefer balanced groups, but never merge unrelated mechanisms just to balance.
- Order groups by total supporting papers, largest first.
