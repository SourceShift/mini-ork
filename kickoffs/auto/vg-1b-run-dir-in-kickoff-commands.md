# VG-1b kickoff commands keep MINI_ORK_RUN_DIR in framework-edit's test verifier

## Goal

Run `sde-3b2-ledger-20261015-142338` failed with every test green (84 passed):
the framework-edit test verifier's `epic-verification-commands-pass` check ran a
kickoff command that writes `--junitxml="${MINI_ORK_RUN_DIR}/ledger-junit.xml"`
and crashed with `OSError: [Errno 30] Read-only file system: '/ledger-junit.xml'`.
`_epic_verification_commands_pass` (`recipes/framework-edit/verifiers/test.py`)
runs kickoff commands under `scrubbed_test_env()`, which drops
`MINI_ORK_RUN_DIR`, so the variable expanded to an empty string. Kickoffs now
declare run-local artifacts under `${MINI_ORK_RUN_DIR}` (the kickoff-contract
verifier, `a7caba16`), so kickoff commands legitimately reference the run dir.
The run dir is a run-local path, not a credential.

All paths are relative to the repository root.

## Mechanism (exact)

1. In `_epic_verification_commands_pass`, after `env = scrubbed_test_env()`, set
   `env["MINI_ORK_RUN_DIR"] = RUN_DIR` (the verifier's own module-level value).
   Credentials stay scrubbed; nothing else is added back.
2. Nothing else in `test.py` changes.

## Files in scope

- recipes/framework-edit/verifiers/test.py
- tests/unit/test_framework_edit_test_verifier_run_dir.py (new)

## Tests (`tests/unit/test_framework_edit_test_verifier_run_dir.py`, hermetic)

- a kickoff command `python3 -c "import os,sys; open(os.environ['MINI_ORK_RUN_DIR'] + '/probe.txt','w').write('ok')"`
  run through `_epic_verification_commands_pass` (load the verifier module with
  `importlib` against a temp run dir + temp patched copy, the way the sibling
  framework-edit verifier tests do) writes `probe.txt` into the temp run dir and
  the check passes;
- a provider credential set in the parent env (e.g. `GLM_API_KEY=x`) is still
  absent from the command's env.

## Verification commands

- `python3 -m pytest -q tests/unit/test_framework_edit_test_verifier_run_dir.py tests/unit/test_framework_edit_static_check.py`

## Self-application measurement

```measure
python3 -m pytest -q tests/unit/test_framework_edit_test_verifier_run_dir.py --junitxml="${MINI_ORK_RUN_DIR}/vg1b-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/vg1b-junit.xml`

## Rules

Edit files directly; do not emit unified diffs. Do not touch static-check.py,
kickoff-contract.py, workflow.yaml, or mini_ork/.
