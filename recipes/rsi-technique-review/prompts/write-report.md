Read only the declared `merged_review` artifact: ranked techniques from a 2026
RSI literature scan (~2000 papers, 10 technique groups), each scored by two
independent reviewers (glm, minimax) with a computed priority, a conservative
consensus verdict, and a disagreement flag.

Write the Markdown decision report for the mini-ork RSI engine to the requested
output path. Required sections, in this order, with these exact headings:

# 2026 RSI Technique Review for mini-ork
## Executive Summary
  5-8 sentences: what the literature says the highest-leverage RSI levers are,
  and what mini-ork should build first and why.
## Technique Groups
  A mermaid `flowchart` mapping the 10 groups to the mini-ork surfaces they
  touch, then one short paragraph per group: what the family is, how crowded the
  evidence is, and its top technique ids.
## Implement Now
  Every `implement-now` technique, in priority order. For each: id and name, the
  mechanism in plain words, the evidence (arxiv ids + the reported numbers), the
  exact mini-ork landing surface, a first slice small enough for one
  framework-edit run, and the measurement that proves it worked.
## Next
  The `next` techniques as a table: id, technique, landing surface, why not now.
## Reviewer Disagreements
  Every disagreement-flagged technique: what each reviewer said and your reading
  of which is right.
## Cautions From the Literature
  Negative findings (collapse, reward hacking, zero-gain skills, poisoning) that
  constrain how the implement-now items must be built.
## Skipped
  One line per skipped cluster of techniques and the reason (mostly: already
  shipped).

Rules:
- Refer to techniques by their ids (e.g. G03-T02) and cite papers only by arxiv
  ids present in the artifact. Invent nothing.
- Keep the consensus verdicts; you may argue in prose that a verdict is wrong,
  but do not relabel it.
