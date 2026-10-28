# Coverage-complete book planning at corpus scale

Collect the top 2000 distinct 2023–2026 papers from the LibWit arXiv corpus on
COVERAGE-COMPLETE PLANNING and CORPUS-SCALE SYNTHESIS: plan-time completeness
contracts, plan verification, set-cover / submodular / facility-location
selection of representatives, hierarchical summarisation and tree reduction over
corpora larger than any context window, plan-then-write long-form generation with
section-level evidence binding, cross-section deduplication and novelty control,
stable item identity across re-planning, and measurable coverage/recall of a
requirement set. Extract the mechanism every paper contributes, cluster them into
ten families, distill each family into implementable techniques, and have two
independent reviewers (glm and minimax — no Anthropic lanes) score each
technique's impact, evidence, effort and novelty against LibWit's SHIPPED coverage
machinery (`context/compose-coverage-state.md`). Deliver a ranked
implement-now / next / watch / skip decision report whose implement-now items are
concrete refactors, adjustments or fixes to the composer.

The measured problem: LibWit's composer can hold 130 required items (50 chapters ×
3–7 sections) but cannot COUNT them. There is no item ledger; the book-level
must-cover list is capped at one aspect per chapter and the extraction prompt
instructs the model to discard the excess; the planner sees a 12 000-character
digest of a closed world that may hold millions of documents; the only hard
completeness gate fires at aspect-tree ROOTS, not at item granularity; and no gate
checks that an item is claimed by *exactly one* chapter. The requirement set is
expected to grow from 130 hand-listed techniques to a ledger derived from a
multi-million-document corpus.

## Scope

- Files in scope: only `recipes/coverage-planning-research/**` (read-only inputs)
  and the run-local `.mini-ork/runs/<id>/` artifacts. No repository source file is
  modified by this run.
- Sources: only the 2023–2026 LibWit/arXiv records returned by
  `recipes/coverage-planning-research/collection-plan.json`; no web results, no
  invented citations.
- The reviewer's ground truth is `context/compose-coverage-state.md`. A technique
  that does not attack a gap named there cannot be implement-now.

## Success Criteria

- `source-corpus.json` holds 2000 distinct unversioned arXiv URLs with dates,
  titles and abstracts.
- Shard extraction covers at least 95% of the corpus (catalog coverage block).
- `clusters.json` has exactly ten groups covering at least 95% of catalog
  techniques.
- `merged-review.json` carries scores from two reviewers.
- `compose-coverage-research.md` has Executive Summary, Implement Now, and both
  appendices; lists all ten groups; cites only corpus arXiv ids and only known
  technique ids.
- At least 6 implement-now techniques, each naming (a) the composer file/contract
  it changes, (b) the first slice small enough for one run, and (c) the measurement
  that proves the coverage gap closed.

## Verification Command

- `python3 recipes/coverage-planning-research/lib/review.py verify --final
  "$MINI_ORK_RUN_DIR/compose-coverage-research.md" --merged
  "$MINI_ORK_RUN_DIR/merged-review.json" --corpus
  "$MINI_ORK_RUN_DIR/source-corpus.json"` exits 0.
