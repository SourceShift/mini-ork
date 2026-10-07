# Reviewer Prompt

Review the proposed framework edit after both deterministic verifiers run.

Inputs:
- `${MINI_ORK_RUN_DIR}/framework-edit.diff`
- `${MINI_ORK_RUN_DIR}/verdict.json`
- Static verifier output.
- Test verifier output.
- Planner and lens reports.

Return one JSON object with:
- `verdict`: `approve`, `revise`, or `reject`
- `reasons`: array of concrete reasons
- `checked_criteria`: array covering artifact names, verifier results, scope,
  and high-blast-radius policy
- `artifact_ref`: `${MINI_ORK_RUN_DIR}/framework-edit.diff`

Reject if `verdict.json` does not use exactly these keys in this order in
documentation and examples: `files_changed`, `tests_pass`, `static_pass`,
`pass`.

---

## Output contract: findings

Every review JSON MUST include a `findings` array. A review that reports no
structured findings is not a review. Each entry:

```json
{
  "file": "<repo-relative path of the changed file>",
  "line": 42,
  "severity": "high",
  "snippet": "<the offending line or fragment, quoted verbatim>",
  "issue": "<one sentence: the defect and why it matters>"
}
```

| Field | Rules |
|---|---|
| `file` | Repo-relative path. Never absolute, never a bare basename. |
| `line` | 1-based line number in the changed file. |
| `severity` | Exactly one of: `high`, `medium`, `low`. |
| `snippet` | The offending text, copied verbatim from the diff or file. |
| `issue` | One sentence. Name the defect, not the category. |

A `needs_revision` verdict (the runtime's name for the `revise` verdict above)
MUST carry at least one finding — a revision request with an empty `findings`
array is a contract violation. A `reject` verdict MUST also carry at least one
finding. An `approve` verdict MAY carry an empty array.

Findings are the machine-readable form of the review and are read by the IDE's
"Your code" view, so they are part of the contract, not decoration: cite the
file and line you actually inspected.
