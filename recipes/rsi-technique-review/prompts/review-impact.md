Read only the declared `review_pack` artifact. It contains (a) every canonical
technique from a 2026 recursive-self-improvement literature scan, grouped into
10 families, each with mechanism, reported results, evidence ids, and an
adoption sketch, and (b) `miniork_state_markdown`: a snapshot of what mini-ork's
RSI engine ALREADY ships, what is measurement-only, and what is known-broken.

You are an independent reviewer deciding what the RSI engine should build next.
Be skeptical: abstract-level claims are unreproduced, and the mini-ork state
notes record that most "procedural knowledge" artifacts give zero gain and that
several shipped mechanisms are measurements whose acting half is not wired.

Score EVERY technique id in the pack. Write one JSON object to the requested
output path, no fences:

{
  "reviewer": "<your lane name, e.g. glm or minimax>",
  "scores": [
    {
      "id": "G01-T01",
      "impact": 1,
      "evidence_confidence": 1,
      "effort": "S | M | L",
      "novelty_vs_shipped": "new | extends | already-shipped",
      "verdict": "implement-now | next | watch | skip",
      "landing_surface": "exact mini-ork module/surface from the state snapshot",
      "rationale": "1-3 sentences citing the state snapshot and the evidence."
    }
  ]
}

Scales:
- impact (1-5): expected effect on mini-ork's verified task success or cost at
  constant verified correctness if it works. 5 = changes what the loop can
  improve at all (e.g. closes an acting half, fixes the verifier bottleneck);
  1 = marginal.
- evidence_confidence (1-5): how much the cited evidence supports the claim
  transferring to a coding-agent task OS.
- effort: S ≤ 2 days, M ≤ 2 weeks, L larger — given the existing surfaces.
- novelty_vs_shipped: check the state snapshot. "already-shipped" when mini-ork
  has the mechanism; "extends" when it builds directly on a shipped piece
  (e.g. wiring an existing measurement's acting half counts as extends).
- verdict: implement-now = high impact, credible evidence, and a clear landing
  spot; next = worth it after the now-list; watch = promising but weak evidence;
  skip = shipped, low impact, or contradicted.

At most 12 techniques may be implement-now. Do not skip any id.
