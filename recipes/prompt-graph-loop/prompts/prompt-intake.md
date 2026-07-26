Read the kickoff and produce exactly one JSON object with these keys:

- `goal`: the requested outcome in one sentence.
- `constraints`: concrete technical, product, time, and safety constraints.
- `desired_artifact`: the output the user expects.
- `open_questions`: only questions that block a safe first draft.

Do not design the graph yet. Preserve the user intent without inventing
requirements. Use empty arrays when the kickoff supplies no values.
