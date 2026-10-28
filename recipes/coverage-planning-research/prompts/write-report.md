Read only the declared `merged_review` artifact: ranked coverage-planning
techniques from a 2023–2026 literature scan (~2000 papers, 10 technique groups),
each scored by two independent reviewers (glm, minimax) with a computed priority,
a conservative consensus verdict, and a disagreement flag.

Write the Markdown decision report for LibWit's BOOK COMPOSER to the requested
output path. Required sections, in this order, with these exact headings:

# Coverage-Complete Book Planning at Corpus Scale
## Executive Summary
  5-8 sentences: what the literature says the highest-leverage coverage mechanisms
  are, and what LibWit should change first and why — framed against the measured
  gap that the composer cannot count its own requirement set.
## Technique Groups
  A mermaid `flowchart` mapping the 10 groups to the composer stages they touch,
  then one short paragraph per group: what the family is, how crowded the evidence
  is, and its top technique ids.
## Implement Now
  Every `implement-now` technique, in priority order. For each: id and name, the
  mechanism in plain words, the evidence (arxiv ids + reported numbers), the exact
  composer file/contract it changes, a first slice small enough for one run, and
  the measurement that proves the gap closed.
## Next
  The `next` techniques as a table: id, technique, composer surface, why not now.
## Reviewer Disagreements
  Every disagreement-flagged technique: what each reviewer said and your reading of
  which is right.
## Cautions From the Literature
  Negative findings (summarisation that silently drops required aspects, selection
  that over-fits one cluster, dedupe that removes needed content, coverage metrics
  that flatter the generator, cost curves that do not scale) that constrain how the
  implement-now items must be built.
## Skipped
  One line per skipped cluster of techniques and the reason (mostly: already
  shipped, or assumes a corpus far smaller than millions of documents).

Rules:
- Refer to techniques by their ids (e.g. G03-T02) and cite papers only by arxiv ids
  present in the artifact. Invent nothing.
- Keep the consensus verdicts; you may argue in prose that a verdict is wrong, but
  do not relabel it.
- Every implement-now item must name a concrete refactor, adjustment or fix — a
  file, a contract, a config constant or a gate — not a research direction.
