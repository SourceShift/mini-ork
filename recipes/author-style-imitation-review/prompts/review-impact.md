Read only the declared `review_pack` artifact. It contains (a) every canonical
technique from a ~1000-paper scan on imitating an author's writing style,
grouped into 8 families, each with a mechanism, reported results, evidence ids,
a transferability note, and an adoption sketch, and (b) `target_state_markdown`:
a written snapshot of what is ALREADY KNOWN TO WORK in this problem, what is
measurement-only, and where the field is known to break.

You are an independent reviewer deciding which techniques actually work. Be
skeptical in a specific way: abstract-level claims are unreproduced, most papers
report a single model/domain, and this literature has a documented pattern of
gains that do not survive a change of author, domain, or model family.

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
      "evidence_verdict": "supported | unclear | overclaimed",
      "verdict": "adopt-now | next | watch | skip",
      "landing_surface": "exact part of a style-replication system this changes",
      "rationale": "1-3 sentences citing the target-state snapshot and the evidence."
    }
  ]
}

Scales:
- impact (1-5): expected improvement in how convincingly the generated text
  reads as the target author, at equal cost and equal content fidelity. 5 =
  changes what is achievable at all (e.g. makes implicit style addressable when
  prompting cannot); 1 = marginal.
- evidence_confidence (1-5): how much the cited evidence supports the claim
  transferring to a new author and a different model family. Multiple
  independent papers, more than one domain, and a reported ablation all raise
  it; a single-paper abstract with no ablation caps it low.
- effort: S ≤ 1 day, M ≤ 1 week, L larger.
- evidence_verdict: `supported` = the cited papers show the effect with numbers
  and at least one control or ablation; `unclear` = plausible but thin,
  single-source, or without a baseline; `overclaimed` = the evidence does not
  support the strength of the claim, or a cited paper contradicts it.
- verdict: adopt-now = high impact, credible evidence, and a clear way to try it
  today; next = worth it after the now-list; watch = promising but weak
  evidence; skip = already standard, low impact, or contradicted.

At most 12 techniques may be adopt-now. Do not skip any id.
