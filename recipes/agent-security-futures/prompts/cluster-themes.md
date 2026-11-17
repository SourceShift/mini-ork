Read only the declared `technique_catalog` artifact. It lists every canonical
agent-security technique extracted from ~2000 2026 papers, each with a stable
`key` (T0001…), a mechanism, and its supporting paper count.

Cluster the techniques into EXACTLY 10 THEMES. A theme is a front of work that
a reader would call a trend — a family of mechanisms moving in one direction,
with one shared adversary, one shared boundary, or one shared control.

Write one JSON object to the requested output path, no fences:

{
  "groups": [
    {
      "name": "Short theme name (e.g. Untrusted content steering the agent)",
      "scope": "2-3 sentences: which mechanisms belong here, what front of work it is, and where the boundary with neighbouring themes is.",
      "technique_keys": ["T0001", "T0007"]
    }
  ]
}

Rules:
- Exactly 10 themes. Group by WHAT is attacked or defended and WHERE it acts,
  e.g. (adapt to what the catalog actually holds; do not force empty families):
  instruction and context control (prompt injection, memory and knowledge
  poisoning, retrieval contamination); the action boundary (tool-call abuse,
  capability restriction, sandboxing, egress); identity, authorization and
  provenance (agent identity, delegation, audit trails, attribution);
  code the agent writes (vulnerable generation, security review, patch
  verification); supply chain and model artifacts; multi-agent trust and
  collusion; detection and monitoring of autonomous behaviour; evaluation and
  benchmark validity; human and organisational response (triage, incident
  response, governance policy, liability); and measured real-world consequence
  (economics, labour, critical infrastructure, safety).
- Every key in the catalog appears in exactly one theme. Do not invent keys.
- Prefer balanced themes, but never merge unrelated mechanisms just to balance.
- Order themes by total supporting papers, largest first.
