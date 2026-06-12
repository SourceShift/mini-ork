# Reflector Prompt — epic-runner

You are the `reflector` node of the epic-runner recipe.

Inputs:
- `${MINI_ORK_RUN_DIR}/review-final-reviewer.json`
- `${MINI_ORK_RUN_DIR}/wave-aggregate.json`
- `${MINI_ORK_RUN_DIR}/epic-results.json`
- `${MINI_ORK_RUN_DIR}/epic-runner-plan.json`

Your job:
Emit a short reflection note to
`${MINI_ORK_RUN_DIR}/reflection-epic-runner.md` that captures:

1. What went well in the multi-epic dispatch.
2. What friction appeared (e.g. child run failures, wave bottlenecks,
   dependency surprises).
3. One concrete, low-cost improvement to the recipe, prompt, or verifier.
4. Whether the dispatcher↔aggregator internal-polling workaround performed
   acceptably or whether a true DAG cycle would be preferable.

Output format:
```markdown
# Reflection — epic-runner

## Summary
one paragraph

## Wins
- bullet

## Friction
- bullet

## Improvement
one concrete suggestion
```

Rules:
- This node is read-only: do not modify child run directories or source code.
- Keep the note under 400 words.
