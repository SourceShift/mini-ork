Read only the declared graph and draft artifacts. Produce exactly one JSON
object with `verdict` (`pass` or `revise`), `claims_checked`,
`graph_completeness`, `output_contract`, `findings`, and `next_action`.

Check that every draft claim has a source or is clearly marked as an
assumption, every graph consumer has a declared input, and the draft satisfies
the requested artifact contract. Make findings specific enough for one repair
pass. A non-empty finding requires `verdict: "revise"`.
