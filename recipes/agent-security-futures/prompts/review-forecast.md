Read only the declared `review_pack` artifact. It contains (a) every canonical
agent-security technique from a 2026 literature scan, grouped into 10 themes,
each with mechanism, reported results, evidence ids, maturity, trajectory, a
proposed defensive move and its test recipe, and (b)
`preparedness_state_markdown`: a written baseline of what a typical team
deploying AI agents today already has and does not have.

You are an independent reviewer judging how the security of agents develops and
what a reader should do about it. Be skeptical on two axes. First: reported
results are claims from the papers' abstracts or retrieved body text, not
reproductions — and security claims are the easiest kind to over-report, because
an attack that works on one toy agent can be written up as a general break, and
a guardrail that catches one benchmark as a defense. Second: a paper stating a
trajectory is not a forecast; treat every projection as the authors' opinion and
score your own confidence in it. Where the literature is thin, say so in the
score rather than inventing certainty.

Write one JSON object to the requested output path, no fences:

{
  "reviewer": "<your lane name, e.g. glm or minimax>",
  "scores": [
    {
      "id": "G01-T01",
      "severity": 1,
      "momentum": 1,
      "evidence_confidence": 1,
      "readiness": "S | M | L",
      "gap_status": "unaddressed | partial | commoditised",
      "verdict": "prepare-now | prepare-next | watch | skip",
      "responsible_owner": "who must act — e.g. 'the team deploying agents', 'security engineering', 'platform/infra', 'model provider', 'regulator/policy'",
      "rationale": "1-3 sentences citing the evidence ids and the preparedness baseline."
    }
  ]
}

Scales:
- severity (1-5): damage if this attack succeeds or this class of failure goes
  unhandled — to the organisation, to the people whose data or actions are
  involved, and to trust in the deployment. 5 = unbounded or irreversible harm
  (data exfiltration at scale, destructive action, critical infrastructure);
  1 = nuisance.
- momentum (1-5): how fast this front is moving in the 2026 literature and in
  what is shipping — a mix of paper volume, reported results improving, and
  maturity rising. 5 = fast-moving with working demonstrations; 1 = static.
- evidence_confidence (1-5): how well the cited evidence supports the claim
  transferring to real deployments of tool-using agents. Penalise toy setups,
  single-paper claims, self-reported attacks without a defense tested.
- readiness: S = a known control exists and is cheap to apply now; M = needs
  integration work; L = research-grade or requires infrastructure nobody has.
- gap_status: read the preparedness baseline — "commoditised" when the control
  is already standard in ordinary engineering practice and only needs to be
  extended to agents; "partial" when a weaker version exists that must be
  strengthened; "unaddressed" when nothing in the baseline does this.
- verdict: prepare-now = high severity, credible evidence, and a control you
  can put in place this quarter; prepare-next = worth the next planning cycle;
  watch = promising but weak or early evidence; skip = commoditised, low
  severity, or contradicted.

At most 15 techniques may be prepare-now. Do not skip any id.
