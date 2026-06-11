# Implementer Prompt

Apply the requested mini-ork change in an isolated worktree.

Rules:
- Preserve user changes in the main checkout.
- Do not edit high-blast-radius files unless `scope_allow` explicitly names
  them.
- Keep changes scoped to the planner and lens findings.
- Produce `${MINI_ORK_RUN_DIR}/framework-edit.diff` as a unified diff ready for
  `git apply`.
- Produce or update `${MINI_ORK_RUN_DIR}/verdict.json` with:
  `{ "files_changed": N, "tests_pass": false, "static_pass": false, "pass": false }`
  before verifiers run.

The diff is the deliverable. Do not commit and do not apply the patch to main.
