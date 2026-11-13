Read only the declared `technique_catalog` artifact. It lists every canonical
security technique extracted from ~2000 2026 papers, each with a stable
`key` (T0001…), a mechanism, and its supporting paper count.

Cluster the techniques into EXACTLY 10 technique groups. Write one JSON object
to the requested output path, no fences:

{
  "groups": [
    {
      "name": "Short group name (e.g. Vulnerability discovery in code an agent wrote)",
      "scope": "2-3 sentences: which mechanisms belong here and where the boundary with neighbouring groups is.",
      "technique_keys": ["T0001", "T0007"]
    }
  ]
}

Rules:
- Exactly 10 groups. Group by MECHANISM FAMILY — WHAT the security technique
  establishes or blocks and WHERE it acts, e.g. (adapt to what the catalog
  actually holds; do not force empty families): detection of vulnerabilities in
  source or dependencies (SAST, taint, CWE classification, LLM code review);
  offensive probing (red teaming, exploit and PoC generation, penetration-test
  agents); verification of a fix (reproduction, regression, patch adequacy);
  fuzzing and input generation; boundary control and sandboxing (capability
  restriction, egress, tool allowlists, isolation); untrusted-content and
  prompt-injection handling; secrets and supply chain; secure code generation
  and vulnerability repair; operations (alert triage, incident response,
  forensics, audit trails); and benchmark / evaluation validity for security
  claims.
- Every key in the catalog appears in exactly one group. Do not invent keys.
- Prefer balanced groups, but never merge unrelated mechanisms just to balance.
- Order groups by total supporting papers, largest first.
