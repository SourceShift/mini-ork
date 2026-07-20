# CLI migration completion audit — loop 3

Status: completed

Last worked on: 2026-07-20 11:33:32 CEST

## Task: validate the pre-retirement oracle against the real Bash dispatcher

Current status: the false-oracle gap was found, corrected, and independently
verified before promotion.

### Subtask: force the retained dispatcher to execute Bash

Current status: completed.

Last worked on: 2026-07-20 11:33:32 CEST

Remaining parts: none.

Evidence:

- The original unit helper invoked `bash bin/mini-ork` without setting
  `MINI_ORK_RUNTIME=bash`; because Python is the runtime-select default, that
  could compare Python with Python instead of exercising the Bash body.
- The untouched dispatcher on pushed main was rerun with
  `MINI_ORK_RUNTIME=bash`. Its dispatcher integration suite passed all 40
  assertions.
- Exact output and exit-code comparisons between true Bash and the new Python
  launcher pass for version, help, doctor, and an unknown command.
- Durable corrected evidence is stored in the CLI migration run directory as
  `true-bash-parity-evidence.log`.

### Subtask: close divergences exposed by the corrected oracle

Current status: completed.

Last worked on: 2026-07-20 11:33:32 CEST

Remaining parts: none.

Implementation details:

- Restored the Bash help contract for `apply` and provider preflight.
- Added native path resolution preserving `lib/paths.sh` precedence for engine
  root, project home, target repo, legacy aliases, and relative engine pointers.
- Preserved the current doctor output byte-for-byte, including the existing
  literal `$_env_var` provider label; product cleanup belongs to a separate
  non-migration change.
- Added regression coverage for symlink invocation, project-local engine
  pointers, canonical project-home output, and the Python-only launcher shape.
- Updated the run-local ledger to describe the complete native path contract.
