# I3 repair: the base replay must include the candidate's test files

## Context

The previous pass for `kickoffs/auto/rsi-i3-bsg-va-wiring.md` is committed on
this branch (HEAD); read it — it is the spec. Its tests are green, but it has a
correctness defect that makes it reject correct fixes.

`recipes/code-fix/verifiers/test.py:_attach_git_worktree_base` builds the base as
`git worktree add --detach <tmp> HEAD`. During verification the candidate's edits
are uncommitted, so the base worktree contains NEITHER the candidate's source
changes (correct) NOR its new or modified test files (wrong). The normal
code-fix shape is "add a regression test + fix the bug". That new test does not
exist on the base, so it is never reported FAILED there, the overlap is empty,
and the verifier returns `tests-do-not-exercise-change` for a correct fix.

## Mechanism (exact spec)

1. After creating the base worktree, overlay the candidate's TEST files onto it:
   every path that is modified, added, or untracked in the candidate working
   tree (`git status --porcelain`, including untracked) AND matches a test
   pattern — any path segment named `tests` or `test`, or basename matching
   `test_*.py` / `*_test.py` / `conftest.py` — is copied from the candidate into
   the base worktree. Non-test paths are never copied. Deleted test files are
   deleted in the base too.
2. Record the overlaid paths in the `replay` payload (`overlaid_tests: [...]`)
   so the audit trail shows exactly what ran on the base.
3. The rest of the semantics are unchanged: pass iff at least one test that
   passes on the candidate FAILS (or ERRORs) on the base-with-candidate-tests.

## Files in scope

- recipes/code-fix/verifiers/test.py
- tests/unit/test_codefix_replay.py

## Tests (add to tests/unit/test_codefix_replay.py)

Using a throwaway git repo where the bug is committed at HEAD:
- candidate fixes the bug AND adds a NEW untracked test file that fails on the
  buggy code → PASS (this is the case the current code gets wrong; it must be a
  new, uncommitted test file, not one committed at HEAD);
- candidate adds a new test file that also passes on the buggy code → FAIL with
  `tests-do-not-exercise-change`;
- a non-test source file changed in the candidate is NOT present in the base;
- the candidate working tree is byte-identical before and after.

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_codefix_replay.py tests/unit/test_certify_oracle.py` exits 0.
- Run it yourself and paste its last line into your final message.

## Rules

Edit files directly; do not emit unified diffs. Do not touch mini_ork/certify/ semantics.
