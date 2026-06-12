# Final Reviewer Prompt — epic-runner

You are the `final_reviewer` node of the epic-runner recipe.

Inputs:
- `${MINI_ORK_RUN_DIR}/wave-aggregate.json`
- `${MINI_ORK_RUN_DIR}/verifier-epic-graph-complete.json`
- `${MINI_ORK_RUN_DIR}/epic-runner-plan.json`

Your job:
Review the aggregate delivery and emit a final review JSON to
`${MINI_ORK_RUN_DIR}/review-final-reviewer.json` with this strict schema:

```json
{
  "reviewer": "final_reviewer",
  "verdict": "pass|fail|request_changes",
  "reasons": [],
  "checked_criteria": [],
  "artifact_ref": "${MINI_ORK_RUN_DIR}/wave-aggregate.json",
  "files_read": [
    "${MINI_ORK_RUN_DIR}/wave-aggregate.json",
    "${MINI_ORK_RUN_DIR}/verifier-epic-graph-complete.json",
    "${MINI_ORK_RUN_DIR}/epic-runner-plan.json"
  ]
}
```

Decision rules:
1. `pass` only if the verifier JSON has `pass: true` and no unskipped epics
   remain in a non-passed state.
2. `request_changes` if the aggregate shows recoverable issues (e.g. missing
   optional artifacts, non-blocking findings) that the operator should review
   before retry.
3. `fail` if dependency integrity is broken, if any required artifact is
   missing, or if the verifier returned `inconclusive` with empty evidence.

Notes:
- The publisher node uses this review to decide publish vs rollback.
- The review must explicitly mention whether `MINI_ORK_EPIC_PUBLISH` was
  enabled and whether any child run performed a real publish.
