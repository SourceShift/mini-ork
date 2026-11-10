Read only the declared `technique_rollup` artifact. Produce a Markdown document
that unifies duplicate techniques across shards and adds every genuinely unique
one. This is the cross-source guidance section of a larger document, not a
replacement for per-paper summaries.

Requirements:
- Group recommendations by the work they help with: goal framing, context and
  evidence, planning, inference-time effort, tools and verification, revision,
  and structured output.
- Each recommendation must be concrete enough to paste into a prompt or a task
  contract. Preserve source IDs in parentheses for every recommendation.
- Merge semantically duplicate instructions into one stronger instruction with
  all supporting source IDs. Keep distinct instructions distinct.
- Include a short limitations section explaining that source-level claims are
  based on the retrieved LibWit metadata, abstracts, and body text (where the
  source record says `evidence_mode: body-text`), and that no reported number was
  reproduced by this run.
- Do not introduce claims or sources absent from the rollup.
