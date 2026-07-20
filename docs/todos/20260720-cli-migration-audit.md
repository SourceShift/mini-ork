# CLI migration completion audit

Status: in progress

Last worked on: 2026-07-20

## Task: close the top-level CLI fork

Current status: pre-retirement baseline is green; implementation has not begun.

### Subtask: preserve the public launcher while retiring Bash

Current status: not started.

Remaining parts:

- Replace the Bash body at `bin/mini-ork` with an executable Python launcher.
- Special-case CLI fork closure so it proves Python ownership instead of
  requiring the public launcher path to be absent.
- Preserve install/symlink behavior and CLI invocation compatibility.

### Subtask: repair already-closed direct command routes

Current status: blocker identified.

Remaining parts:

- Route direct `plan` and `reflect` calls in `mini_ork_cli.main` to their native
  Python modules.
- Ensure direct `verify` and `classify` remain native.
- Add regression coverage proving no direct route targets retired files.

### Subtask: close CLI runtime seams

Current status: discovery in progress.

Remaining parts:

- Map all runtime callers of `bin/mini-ork` and distinguish public command
  usage from Bash-implementation coupling.
- Preserve the execute command seam until the execute fork closes.
- Audit deadline, config snapshot, repo-integrity, and rubric pre-screen shell
  seams for explicit behavior preservation.

### Subtask: satisfy migration gates

Current status: pre-retirement oracle passed.

Remaining parts:

- Produce the static-feature ledger and CLI integration map.
- Pass standalone unit tests, dispatcher integration, CLI feature acceptance,
  focused Pyright, parity, ledger shape, and CLI-specialized fork closure.
- Run the completion requirements audit twice and update the migration handoff
  and tracker before marking this task complete.

