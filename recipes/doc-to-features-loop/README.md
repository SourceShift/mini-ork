# doc-to-features-loop

`doc-to-features-loop` is an outer recipe for turning a markdown business,
product, or technical specification into a ranked queue of implementable
features. It reads the source document through separate surface and deep
extraction lenses, merges those findings into `feature-index.json`, checks
that important implementation ideas cite modern techniques from arxiv-libwit,
and then dispatches P0 features one by one.

The inner implementation loop is `recursive-validate-impl`. This recipe does
not implement every extracted feature itself; it writes per-feature kickoffs
for the inner loop, waits for the child verdicts, and aggregates them into
`aggregate-verdict.json`. Failed child runs feed a reflector and replanner so
the backlog can be refined before another iteration.

Use this recipe when the input is a broad document with many implied tasks, not
when a single implementation task is already clear. The expected run artifacts
are `feature-index.json`, `aggregate-verdict.json`, `reflector.json`, and
`replan.json` in `${MINI_ORK_RUN_DIR}`.
