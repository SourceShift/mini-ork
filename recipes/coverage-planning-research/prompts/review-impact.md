Read only the declared `review_pack` artifact. It contains (a) every canonical
coverage-planning technique from a 2023–2026 literature scan, grouped into 10
families, each with mechanism, reported results, evidence ids, a composer surface
and an adoption sketch, and (b) `composer_state_markdown`: a snapshot of what
LibWit's composer ALREADY ships for coverage, what is measurement-only, and what
is known-broken (with file anchors).

You are an independent reviewer deciding what the composer should build next. Be
skeptical on two axes. First: abstract-level claims are unreproduced. Second, and
specific to this review — the composer's state notes record a MEASURED coverage
gap: it can hold 130 required items but has no ledger that counts them; the
book-level must-cover list is capped at one aspect per chapter and the extraction
prompt instructs the model to discard the excess; the planner sees a
12 000-character digest while the closed world may hold millions of documents; the
only hard completeness gate fires at aspect-tree roots, not item granularity; and
nothing checks that an item is claimed by exactly one chapter. Score techniques by
how directly they attack THAT gap, at corpus scale.

Score EVERY technique id in the pack. Write one JSON object to the requested
output path, no fences. Each score entry:

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
      "landing_surface": "exact composer stage/contract from the state snapshot this changes",
      "rationale": "1-3 sentences citing the state snapshot and the evidence."
    }
  ]
}

Scales:
- impact (1-5): how much closer it moves the composer to guaranteed exactly-once
  coverage over a multi-million-document corpus, measured against the gaps in
  `composer_state_markdown`. 5 = closes the ledger/counting gap or makes coverage
  checkable at item granularity; 1 = marginal. Not how interesting the paper is.
- evidence_confidence (1-5): how much the cited evidence supports the claim
  transferring to a book composer at our corpus and context scale.
- effort: S = one file or one contract change; M = a service plus its tests;
  L = a new stage or schema.
- novelty_vs_shipped: check the state snapshot. "already-shipped" when the
  composer has the mechanism AND it is exercised in the path (inert/opt-in
  counts as "extends"); "extends" when it builds directly on a shipped piece.
- verdict: implement-now = high impact, credible evidence, and a clear landing
  spot; next = worth it after the now-list; watch = sound idea, but the scale it
  assumes is far below millions of documents; skip = shipped or superseded
  within the pack.

At most 12 techniques may be implement-now. Do not skip any id.
