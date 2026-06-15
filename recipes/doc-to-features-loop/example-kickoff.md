# Example Kickoff: doc-to-features-loop

## Source doc:

`/absolute/path/to/business-or-product-plan.md`

## Hard rules

- Extract explicit feature lists, tables, milestones, and tier labels.
- Extract implicit platform, compliance, observability, data, and operations
  work even when the source doc only hints at it.
- Every P0 feature must include non-empty `modern_techniques_refs` gathered via
  arxiv-search-tool before dispatch.
- Dispatch each P0 feature through `recursive-validate-impl`; do not implement
  broad document features directly in this outer loop.
- Keep child patches scoped to each generated child kickoff.

## Success

- `${MINI_ORK_RUN_DIR}/feature-index.json` contains at least five ranked
  features unless `MO_DOC_LOOP_MIN_FEATURES` overrides the minimum.
- `${MINI_ORK_RUN_DIR}/aggregate-verdict.json` has shape
  `{total, passed, failed, pending, pass_rate, features[]}`.
- All P0 child runs either pass or are represented as failed/pending with
  enough evidence for reflector and replanner.
