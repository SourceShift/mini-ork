# Planner — Bug audit

You are the planner for a bug-only audit. Read the kickoff (which
embeds the validated feature inventory from Phase 1) and produce a
5-7 step plan covering:

Kickoff content:
```text
{{KICKOFF_CONTENT}}
```

1. Bug classes in scope (correctness, race, security, observability,
   contract drift, fail-open hazards, dead-code-after-cutover)
2. What the panel definition of a "bug" is — must be a CONCRETE
   defect, NOT a wishlist item or a future improvement
3. Per-feature coverage targets — at least 1 bug-find pass per
   feature listed in the kickoff
4. Synthesis rules — consensus markers, severity grading, false-positive
   filter
5. Output target: unified bug report at
   `${MINI_ORK_RUN_DIR}/synthesis.md`

Keep the plan short. Each lens already knows what kind of bug to look
for — your job is to lock the bug definition + thresholds + format.

CRITICAL: this audit is REPORT-ONLY. No code edits. No fix proposals
should land in the synthesis as actionable patches — only AS bug
descriptions with file:line + reproduction sketch + impact.

Output to `${MINI_ORK_RUN_DIR}/plan.md`. Markdown, ≤500 words.
