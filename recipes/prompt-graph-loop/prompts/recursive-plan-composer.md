Read the declared artifact inputs and produce exactly one JSON object named an
agent graph. It must contain `nodes`, `edges`, `artifacts`, `risks`, and
`iteration`. Every node needs an ID, role, purpose, required inputs, and named
outputs. Every edge must name its producer output and consumer input.

When `refinement_prompt` is present, change only the parts needed to address it
and increment `iteration`. Preserve valid prior topology. Keep the graph small
enough to verify and make all human decisions explicit.
