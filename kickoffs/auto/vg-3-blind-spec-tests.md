# VG-3 blind spec tests for framework-edit (verifier gap F3)

## Goal

framework-edit runs pass when the implementer's OWN tests pass, and those tests
encode the implementer's interpretation, not the kickoff. Evidence: SDE-1 pass 1
invented its own failure taxonomy and its tests asserted it; SDE-3a pass 1 wrote
a test that asserted a bug (a parked run stranded by a later sweep). A second
model, different from the implementer's, that writes tests from the kickoff ALONE —
before any implementation exists — catches exactly these. This adds that author
node and a verifier that runs its tests against the change.

All paths are relative to the repository root.

## Mechanism (exact spec)

1. **Author node** in `recipes/framework-edit/workflow.yaml`:
   `spec_test_author` — `type: researcher`, `model_lane: glm_lens` (a different
   model — GLM — from the implementer's `codex_lens`, DeepSeek), `prompt_ref: prompts/spec-test-author.md`,
   `dispatch_mode: serial`. Edges: `planner → spec_test_author (depends_on)`,
   `spec_test_author → implementer (depends_on)`. Because it finishes before the
   implementer starts, it is structurally blind to the implementation.
2. **Prompt** `recipes/framework-edit/prompts/spec-test-author.md`: from the
   kickoff's Mechanism / Tests / Success sections only, write pytest files into
   `${MINI_ORK_RUN_DIR}/spec-tests/test_spec_*.py` that import the modules the
   kickoff names; every test function's docstring starts with `Kickoff:` and
   quotes the clause it checks; prefer hermetic tests (tmp dirs, stubs); write
   nothing when the kickoff has no testable behaviour. Never read or edit the
   target's existing tests.
3. **Implementer prompt** (`prompts/implementer.md`): add that
   `${MINI_ORK_RUN_DIR}/spec-tests/` holds independent acceptance tests the
   change must pass, and that they must never be modified.
4. **Verifier** `recipes/framework-edit/verifiers/spec-tests.py` (conventions of
   the sibling verifiers; writes `verifier_spec-tests.json`; exit 0/1):
   - no `spec-tests/` or no test files → pass with note "no spec tests authored";
   - tamper check: any spec-test file whose mtime is later than the
     `spec_test_author` node's `node_end` `created_at` in `run_events` → fail
     ("spec tests modified after authoring"), naming the files;
   - test functions without a `Kickoff:` docstring are deselected and reported,
     never run;
   - run `python3 -m pytest -q <run_dir>/spec-tests -p no:cacheprovider` with
     cwd = `MO_TARGET_CWD` and `PYTHONPATH` = that dir, credentials scrubbed
     (reuse `mini_ork.verify.test_env.scrubbed_test_env` as
     `kickoff-contract.py` does), timeout `MO_SPEC_TESTS_TIMEOUT_S` (default 900);
     any failure → fail with the failing test ids and their `Kickoff:` quotes in
     the evidence (so the implementer's revise round sees the clause).
5. **Wiring:** node `spec_test_verifier` (`type: verifier`, `verifier_ref:
   verifiers/spec-tests.py`) with edges `implementer → spec_test_verifier
   (verifies)`, `spec_test_verifier → reviewer (depends_on)`,
   `spec_test_verifier → rollback (escalates_to)`,
   `spec_test_verifier → implementer (retries, max_rounds: 2)`.
6. Keep the recipe's shape checks honest: update
   `verifiers/framework-edit-shape.py` (exact node count and lane bindings) and
   `tests/unit/test_revise_loop.py` (framework-edit retry sources) for the two
   new nodes.

## Files in scope

- recipes/framework-edit/workflow.yaml
- recipes/framework-edit/prompts/spec-test-author.md (new)
- recipes/framework-edit/prompts/implementer.md
- recipes/framework-edit/verifiers/spec-tests.py (new)
- recipes/framework-edit/verifiers/framework-edit-shape.py
- tests/unit/test_revise_loop.py
- tests/unit/test_framework_edit_spec_tests.py (new)

## Tests (`tests/unit/test_framework_edit_spec_tests.py`, hermetic subprocess runs of the verifier against temp run dirs + temp targets)

- passing spec tests → pass; a failing one → fail, and the evidence names the
  test and its `Kickoff:` quote;
- a spec file with mtime after the author's `node_end` → fail (tamper);
- a test without a `Kickoff:` docstring is deselected and reported, not run;
- no spec-tests dir → pass with the note;
- a timeout → fail;
- the workflow compiles (`mini_ork.workflow.compiler.compile_workflow`), the
  author precedes the implementer, and the verifier has its four edges.

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_framework_edit_spec_tests.py tests/unit/test_revise_loop.py tests/unit/test_framework_edit_kickoff_contract.py --junitxml="${MINI_ORK_RUN_DIR}/spec-tests-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/spec-tests-junit.xml`

## Success criteria

- `python3.11 -m pytest -q tests/unit/test_framework_edit_spec_tests.py tests/unit/test_revise_loop.py tests/unit/test_framework_edit_kickoff_contract.py tests/unit/test_framework_edit_static_check.py` passes.

## Rules

Edit files directly; do not emit unified diffs. Do not touch static-check.py,
kickoff-contract.py, test.py, execute.py or execute_handlers.py.
