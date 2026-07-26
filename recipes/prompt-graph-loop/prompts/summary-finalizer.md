Read the declared graph, source corpus, draft, verification report, and human
decision. Produce exactly one JSON object with `approval`, `summaries`,
`verified_claims`, `open_limitations`, and `source_coverage`.

`summaries` must contain one finalized record for each completed stage. Each
record needs `stage`, `summary`, `source_artifacts`, `verification_status`, and
`limitations`. Only summarize material in the declared inputs. Preserve source
URLs and dates from the source corpus, and do not convert an assumption into a
verified claim. The human decision must be `approved`.
