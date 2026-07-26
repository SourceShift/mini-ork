Read the semantic signals and retrieve the evidence needed to satisfy their
research requirements. Produce exactly one JSON object with `source_count`,
`required_source_count`, `sources`, `coverage_gaps`, and `search_notes`.

Each `sources` entry must contain `title`, `url`, `published_at`,
`retrieved_at`, and `relevance`. When the request requires a minimum corpus,
such as 200 current sources, do not claim the corpus is complete until the
manifest contains at least that many distinct URLs. Report shortfall and
coverage gaps instead of inventing citations or dates.
