# Author-style imitation: which techniques actually work?

Collect at least 1000 distinct papers from the LibWit arXiv corpus on imitating
an individual author's writing style — personal style modeling, few-shot style
conditioning, style transfer, authorship attribution and verification,
stylometry, style-preserving rewriting, per-author adaptation, and the
evaluation and failure modes around all of them. Extract the technique every
paper contributes, cluster those techniques into eight mechanism families,
distill each family into implementable techniques, have two independent
reviewers (glm-lens and minimax-lens; no Anthropic lanes) score every technique's
impact, evidence strength, effort, and whether the evidence supports the claim,
and deliver a ranked report naming the techniques that work.

The anchor is **arxiv:2509.14543** (*Catch Me If You Can? Not Yet: LLMs Still
Struggle to Imitate the Implicit Writing Styles of Everyday Authors*, EMNLP
2025) — >40,000 generations per model, >400 real-world authors, four metrics
(authorship attribution, authorship verification, style matching, AI detection).
Treat its finding as the bar: structured genres (news, email) are approximated,
informal genres (blogs, forums) are not, and implicit style is where the gap
lives.

## Scope

- Files in scope: only `recipes/author-style-imitation-review/**` (read-only
  inputs) and the run-local `.mini-ork/runs/<id>/` artifacts. No repository
  source file is modified by this run.
- Sources: only the LibWit/arXiv records returned by
  `recipes/author-style-imitation-review/collection-plan.json` — 50 queries in 5
  batches of 10, published 2023-01-01 or later. No web results, no invented
  citations, no full-text claims that are not in the retrieved metadata.

## Success Criteria

- `source-corpus.json` holds at least 1000 distinct unversioned arXiv URLs with
  dates, titles, and abstracts, merged from all five searcher batches. The
  anchor paper must appear in it.
- `technique-catalog.json` covers at least 95% of the corpus (its `coverage`
  block states covered/expected), and carries a technique count in the
  hundreds.
- `clusters.json` has exactly eight groups covering at least 95% of catalog
  techniques.
- `merged-review.json` carries scores from two independent reviewers, a
  `replicated_technique_count`, and disagreement flags.
- `author-style-imitation-review.md` has Executive Summary, Techniques That
  Work, and both appendices; lists all eight groups; cites only corpus arXiv ids
  and only known technique ids; and its Techniques That Work section states the
  paper count behind each technique.

## Verification Command

- `python3 recipes/author-style-imitation-review/lib/style_pipeline.py verify
  --final "$MINI_ORK_RUN_DIR/author-style-imitation-review.md" --merged
  "$MINI_ORK_RUN_DIR/merged-review.json" --corpus
  "$MINI_ORK_RUN_DIR/source-corpus.json"` exits 0.
