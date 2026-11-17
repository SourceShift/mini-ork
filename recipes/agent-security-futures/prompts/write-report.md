Read the declared `merged_review` artifact. It holds the ranked
agent-security techniques of a 2026 literature scan: 10 themes, each with its
scope and techniques (ids `G01-T01`…), each technique carrying mechanism,
reported results, maturity, trajectory, defensive move, test recipe, evidence
ids, and the consensus of two independent reviewers — severity, momentum,
evidence confidence, readiness, gap status, and a prepare verdict.

Write the Markdown briefing to the requested output path. The reader is not a
mini-ork operator: they are an engineer or decision-maker who wants to know
where agent security is going, what it will do to people, and what to do about
it. Required sections, in this order, with these exact headings:

# Agent Security 2026 — Where It Is Going and How to Prepare

## Executive Summary
  8-12 sentences: the two or three fronts moving fastest, the one boundary that
  is currently failing, what the next 12-36 months most likely look like, and
  the first three moves a reader should make. End with one sentence a
  non-specialist could repeat.

## Trending Now
  A mermaid `flowchart LR` from attacker moves → the boundary under pressure →
  the defenses that answer them, using theme names (and technique ids where a
  node is one technique). Then one short subsection per theme, largest first:
  3-6 sentences on what the theme is, the strongest reported numbers, and what
  moved in 2026. Reference techniques by id; do not create headings that start
  with `### G` followed by two digits — use `####` prose headings instead.

## Forecast
  The forward-looking core. A table: horizon (12 months / 36 months / beyond),
  the change expected, the driver it follows from, the confidence
  (low/medium/high) and the indicator that would confirm or refute it. Then,
  for each of the three highest-momentum themes, one paragraph: what improves
  for attackers, what improves for defenders, and the most likely failure that
  results. Separate clearly what the literature measured from what you are
  extrapolating; never present an extrapolation as a result.

## Human Impact
  Who is actually affected and how: people whose data or money an agent touches,
  workers whose tasks agents take over, organisations that deploy agents, and
  the public exposed to agent-caused incidents at scale. For each, the concrete
  consequence the evidence supports, the consequence you forecast, and the
  asymmetry (who bears the risk versus who gets the benefit). Where the corpus
  says nothing about a consequence, say that instead of guessing.

## How to Prepare
  Ordered by the merged ranking, not by theme. For each `prepare-now`
  technique: id and name, the control, where it sits, what it must reject or
  flag, how to prove it works (the test recipe), the owner, and the cost. Then
  a short subsection for `prepare-next`, and one paragraph on what to stop doing.

## What Would Change This Forecast
  Bulleted: the observations that would invalidate the top forecasts, and the
  cheapest way to watch for each.

Rules:
- Refer to techniques by id (G01-T01) and cite papers only by arxiv ids present
  in the artifact. Invent nothing: no products, no numbers, no standards, no
  organisations that the artifact does not contain.
- Keep the reviewers' consensus verdicts. Argue in prose if you disagree with a
  score, but do not relabel it.
- Plain, precise prose; no marketing language, no hedging padding. Short
  paragraphs. Every claim that matters carries an id or an arxiv id.
- Do not write `## Appendix`, `## Evidence Limits`, or any ranking table — the
  assembler appends those. Do not cite a technique id that is absent from the
  artifact.
