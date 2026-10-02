Read only the declared `review_pack` artifact. It contains (a) every canonical
technique from a 2026 literature scan on coordinating concurrent agents, grouped
into 10 families, each with mechanism, reported results, evidence ids, and an
adoption sketch, and (b) `miniork_state_markdown`: a snapshot of the
coordination machinery mini-ork and ContextNest ALREADY ship, where it is
checked, and the observed failures it misses.

You are an independent reviewer deciding what to build for a harness-agnostic
CROSS-AGENT OVERLAP PROTOCOL: on every agent turn, detect that independent
agents/loops/sessions are touching overlapping files, settings, shared state, or
the same topic, and get them arranged before they continue. Be skeptical:
abstract-level claims are unreproduced, many papers evaluate toy or simulated
environments, and coordination overhead can cancel its own gains.

Score EVERY technique id in the pack. Write one JSON object to the requested
output path, no fences:

{
  "reviewer": "<your lane name, e.g. glm, minimax, or deepseek>",
  "scores": [
    {
      "id": "G01-T01",
      "impact": 1,
      "evidence_confidence": 1,
      "effort": "S | M | L",
      "novelty_vs_shipped": "new | extends | already-shipped",
      "verdict": "implement-now | next | watch | skip",
      "landing_surface": "exact mini-ork/ContextNest module/surface from the state snapshot",
      "rationale": "1-3 sentences citing the state snapshot and the evidence."
    }
  ]
}

Scales:
- impact (1-5): expected reduction in silent overlaps (duplicated work,
  clobbered files/settings, misrouted messages, wasted runs) across concurrent
  agents if it works. 5 = closes a whole class of the failures in the snapshot
  (e.g. turns start-only checks into per-turn checks); 1 = marginal.
- evidence_confidence (1-5): how much the cited evidence supports the claim
  transferring to concurrent coding agents on a shared repo + shared config.
- effort: S ≤ 2 days, M ≤ 2 weeks, L larger — given the existing surfaces.
- novelty_vs_shipped: check the state snapshot. "already-shipped" when the
  mechanism exists; "extends" when it builds directly on a shipped piece (e.g.
  re-checking an existing start-time claim on every turn counts as extends).
- verdict: implement-now = high impact, credible evidence, and a clear landing
  spot; next = worth it after the now-list; watch = promising but weak evidence;
  skip = shipped, low impact, or contradicted.

At most 12 techniques may be implement-now. Do not skip any id.
