# Implementer Prompt

Apply the requested mini-ork change in an isolated worktree.

Rules:
- Preserve user changes in the main checkout.
- Do not edit high-blast-radius files unless `scope_allow` explicitly names
  them.
- Keep changes scoped to the planner and lens findings.
- `${MINI_ORK_RUN_DIR}/spec-tests/` holds independent acceptance tests written
  from the kickoff BEFORE this change existed. They are the run's blind spec
  suite: your change must pass them, and they must NEVER be modified. Editing,
  adding or deleting a file there is detected (a file touched after the author
  node ended, or one the author listed that is gone, fails the spec-tests
  verifier outright) and is not a way to make a failing clause pass — fix the
  implementation instead.
- Produce `${MINI_ORK_RUN_DIR}/framework-edit.diff` as a unified diff ready for
  `git apply`.
- Do NOT write `${MINI_ORK_RUN_DIR}/verdict.json`. The verifier nodes own that
  file end-to-end. A pre-written `pass: false` from the implementer becomes
  the final verdict whenever a verifier crashes or skips, masking real
  results — that footgun is closed by leaving verdict.json absent until
  the verifier writes it.

The diff is the deliverable. Do not commit and do not apply the patch to main.
Never commit, rebase, reset, pull, stash, or check out another ref in the target:
HEAD must stay on the run's starting commit (`${MINI_ORK_RUN_DIR}/pre-implementer-ref`).
The engine re-derives the diff from the working tree against that commit, and a
moved HEAD fails the implementer node (`impl_moved_base`). An out-of-date base is
not yours to fix: the merge step rebases.
