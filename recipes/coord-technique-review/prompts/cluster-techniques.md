Read only the declared `technique_catalog` artifact. It lists every canonical
technique extracted from ~1000 2026 papers on coordinating concurrent agents
(detecting, preventing, and resolving overlap/conflict on shared files, config,
memory/state, and tasks), each with a stable `key` (T0001…), a mechanism, and
its supporting paper count.

Cluster the techniques into EXACTLY 10 technique groups. Write one JSON object
to the requested output path, no fences:

{
  "groups": [
    {
      "name": "Short group name (e.g. Claims, leases & locks)",
      "scope": "2-3 sentences: which mechanisms belong here and where the boundary with neighbouring groups is.",
      "technique_keys": ["T0001", "T0007"]
    }
  ]
}

Rules:
- Exactly 10 groups. Group by MECHANISM FAMILY — what the technique does in a
  multi-agent system (e.g. how agents publish intent/claims, how access to a
  shared resource is serialized or made optimistic, how shared state/memory
  stays consistent, how overlap/conflict is DETECTED at runtime, how a detected
  conflict is RESOLVED/arbitrated, how work is allocated to avoid duplication,
  how agents communicate/hand off, how actions are monitored/attributed, how
  agents are isolated, how coordination failures are diagnosed/evaluated) —
  adapt the partition to what the catalog actually contains; do not force empty
  families.
- Every key in the catalog appears in exactly one group. Do not invent keys.
- Prefer balanced groups, but never merge unrelated mechanisms just to balance.
- Order groups by total supporting papers, largest first.
