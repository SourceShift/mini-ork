# Implementer Prompt

Apply the requested mini-ork change in an isolated worktree.

Rules:
- Preserve user changes in the main checkout.
- Do not edit high-blast-radius files unless `scope_allow` explicitly names
  them.
- Keep changes scoped to the planner and lens findings.
- Produce `${MINI_ORK_RUN_DIR}/framework-edit.diff` as a unified diff ready for
  `git apply`.
- Do NOT write `${MINI_ORK_RUN_DIR}/verdict.json`. The verifier nodes own that
  file end-to-end. A pre-written `pass: false` from the implementer becomes
  the final verdict whenever a verifier crashes or skips, masking real
  results — that footgun is closed by leaving verdict.json absent until
  the verifier writes it.

The diff is the deliverable. Do not commit and do not apply the patch to main.
