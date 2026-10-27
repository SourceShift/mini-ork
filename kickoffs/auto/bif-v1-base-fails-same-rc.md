# V1: a base failure excuses a patched failure only when it is the same failure

## Goal

The framework-edit test verifier runs the kickoff's `## Verification commands`
on the patched copy (`_epic_verification_commands_pass`,
`recipes/framework-edit/verifiers/test.py` ~678-743). When a command fails
there, it replays the command on the base copy; if the base fails too, the
failure is recorded as a non-gating `base-fails` abstention and the check
PASSES.

The base copy does not have the test files the diff adds. A verification
command that names a new test file exits **4** on the base (pytest: file not
found, a usage error) whatever the patched copy did, so a real patched test
failure (exit **1**) is excused as "shared with the base".

Measured on every `verifier_test.json` under the home's runs, 2026-10-27: 16
`base-fails` excusals, 10 of them `patched rc=1, base rc=4`, including
`eco6-k13-remove-node-gates-20261025-130443` (an incident the reviewer also
missed), `rev6-r2a-prepush-gate-20261026-133644` and
`bif-f2-precheck-and-failed-checks-20261027-160624`, whose in-run reviewer
caught the red command the verifier passed.

A base failure is only evidence that the patched failure is not the diff's
when the base fails THE SAME WAY.

All paths are relative to the repository root.

## Mechanism (exact spec)

In `_epic_verification_commands_pass` (`recipes/framework-edit/verifiers/test.py`):

1. After `base_rc, base_timed_out = _run_acceptance_command(BASE_WORKTREE, cmd)`
   and the existing timeout check, keep the `base-fails` abstention ONLY when
   `base_rc == rc` (both non-zero, same exit code). Its row, message and
   `continue` are unchanged.
2. When `base_rc != 0` and `base_rc != rc`, write one evidence line
   `  [base-differs] \`<cmd>\` fails on the patched copy (rc=<rc>) and differently on the base copy (rc=<base_rc>): the base cannot run the same check, so the failure stays gating`
   and `return False`.
3. `base_rc == 0` (a patched-only failure) stays `return False`, as today.

Nothing else in the verifier changes.

## Files in scope (touch ONLY these)

- `recipes/framework-edit/verifiers/test.py`
- `tests/unit/test_fe_verifier_hermetic.py`

Do not modify `recipes/framework-edit/verifiers/static-check.py` (frozen kernel), any other verifier, `mini_ork/`, `docs/`, or any path in `config/kernel-frozen.yaml`. Every existing test must pass unchanged.

## Tests (append to `tests/unit/test_fe_verifier_hermetic.py`, hermetic)

Follow `test_failure_shared_with_the_base_copy_abstains_without_gating`
(~209-228) and its helpers (`_target_repo`, `_run_dir`, `_run_verifier`,
`_envelope`):

1. **Different exit codes stay gating.** A command that exits 1 on the
   patched copy and 4 on the base copy:
   `{ pwd; cat marker.txt; } >> <dump>; if grep -q BASE marker.txt; then exit 4; else exit 1; fi`
   → no check has `failure_kind == "base-fails"`, `data["pass"] is False`,
   `data["failed_checks"]` is non-empty, and the evidence log contains
   `[base-differs]`.
2. **Same exit code still abstains.** The existing exit-9 test keeps passing
   unchanged (re-run, do not edit).
3. **Patched-only failure still gating.** The existing
   `test_failure_only_on_the_patched_copy_is_a_regression` keeps passing.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_fe_verifier_hermetic.py tests/unit/test_framework_edit_static_check.py tests/unit/test_verifier_envelope_honoured.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_fe_verifier_hermetic.py --junitxml="${MINI_ORK_RUN_DIR}/bif-v1-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/bif-v1-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test is unchanged.
- `ruff check --select F,E9 recipes/framework-edit/verifiers/test.py tests/unit/test_fe_verifier_hermetic.py` is clean.
- A patched failure is excused only by a base failure with the same exit code.
