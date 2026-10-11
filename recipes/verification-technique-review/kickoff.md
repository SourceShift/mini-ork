# 2026 verification technique review for mini-ork's verification & anchoring stack

Collect the top 2000 distinct 2026 papers from the LibWit arXiv corpus on
VERIFICATION AND ANCHORING of AI systems — execution/test-based verification,
oracle and ground-truth construction, metamorphic / property-based / invariant
checking, formal and certificate-based verification, differential and mutation
testing, verifier and reward-model construction and VALIDITY AUDITING,
reward-hacking and specification-gaming detection, LLM-judge reliability and
calibration, abstention / selective prediction / uncertainty quantification,
grounding / citation / claim / trajectory verification, and benchmark and
evaluation validity. Extract the techniques every paper contributes, cluster
them into ten technique groups, distill each group into implementable
techniques mapped to mini-ork's verification surfaces, and have two independent
reviewers (glm and minimax — no Anthropic lanes) score each technique's impact,
evidence, effort, and novelty against mini-ork's shipped verification state.
Deliver a ranked implement-now / next / watch / skip decision report.

mini-ork's verdict is EXECUTION-ANCHORED: a candidate change is judged by what
the code actually did, never by a model's opinion, and an LLM judge may only
veto, never approve. The measured gap this review must attack: the loop
certifies NON-REGRESSION, not CORRECTNESS (on SWE-bench the cheap lane produced
3/6 correct fixes and the gate rejected all 3); the solve-time oracle has a
measured recall hole (~84%); and execution-anchored single tests are gameable
through coverage gaps.

## Scope

- Files in scope: only `recipes/verification-technique-review/**` (read-only
  inputs) and the run-local `.mini-ork/runs/<id>/` artifacts. No repository
  source file is modified by this run.
- Sources: only the 2026 LibWit/arXiv records returned by
  `recipes/verification-technique-review/collection-plan.json`; no web results,
  no invented citations.

## Success Criteria

- `source-corpus.json` holds 2000 distinct unversioned arXiv URLs with dates,
  titles, and abstracts.
- Shard extraction covers at least 95% of the corpus
  (`technique-catalog.json` coverage block).
- `clusters.json` has exactly ten groups covering at least 95% of catalog
  techniques.
- `merged-review.json` carries scores from two reviewers.
- `verification-technique-review.md` has Executive Summary, Implement Now, and
  both appendices; lists all ten groups; cites only corpus arXiv ids and only
  known technique ids.

## Verification Command

- `python3 recipes/verification-technique-review/lib/review.py verify --final
  "$MINI_ORK_RUN_DIR/verification-technique-review.md" --merged
  "$MINI_ORK_RUN_DIR/merged-review.json" --corpus
  "$MINI_ORK_RUN_DIR/source-corpus.json"` exits 0.
