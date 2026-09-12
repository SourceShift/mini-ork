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
