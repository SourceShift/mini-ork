Read only the declared `review_pack` artifact. It contains (a) every canonical
security technique from a 2026 literature scan, grouped into 10 families, each
with mechanism, reported results, evidence ids, and an adoption sketch, and (b)
`miniork_state_markdown`: a snapshot of what mini-ork's security posture ALREADY
ships, what is measurement-only, and what is known-open.

You are an independent reviewer deciding what mini-ork should build next. Be
skeptical on two axes. First: reported results are claims from the papers'
abstracts or retrieved body text, not reproductions — and security claims are
the easiest kind to over-report, because a scanner that finds one vuln in a toy
repo can be written up as a general detector. Second, and specific to this
review — mini-ork's own state notes record what is actually missing: the
`code-fix` verifier set checks tests, types and lint and has NO security check;
secret scanning covers credentials only; nothing in the pipeline detects a
vulnerability an agent just introduced, and nothing measures whether the agent
would follow instructions planted in a repo file or an issue body. Score
techniques by how directly they attack THAT hole, at the cost of one verifier
node.

Write one JSON object to the requested output path, no fences:

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
- impact (1-5): expected effect on mini-ork's ability to find a security defect
  it currently cannot find, block one it cannot block, or stop a false security
  claim, at constant or lower cost. 5 = closes the missing-security-check hole
  or a measured evasion/false-positive failure; 1 = marginal.
- evidence_confidence (1-5): how much the cited evidence supports the claim
  transferring to a coding-agent task OS that ships its own patches.
- effort: S ≤ 2 days, M ≤ 2 weeks, L larger — given the existing surfaces.
- novelty_vs_shipped: check the state snapshot. "already-shipped" when mini-ork
  has the mechanism; "extends" when it builds directly on a shipped piece
  (e.g. adding a security term to an existing verifier node counts as extends).
- verdict: implement-now = high impact, credible evidence, and a clear landing
  spot; next = worth it after the now-list; watch = promising but weak evidence;
  skip = shipped, low impact, or contradicted.

At most 12 techniques may be implement-now. Do not skip any id.
