# CLI migration completion audit

Status: completed

Last worked on: 2026-07-20

## Task: close the top-level CLI fork

Current status: the isolated review proposal closes the CLI fork; no source
change has been committed or applied to the real checkout.

### Subtask: preserve the public launcher while retiring Bash

Current status: completed.

Remaining parts: none.

Implementation details:

- `bin/mini-ork` is an executable Python launcher that resolves symlinks and
  imports `mini_ork.ported.mini_ork_cli.main` without runtime selection.
- CLI-specialized closure proves the public path exists and is Python-only.
- Golden launcher tests cover exact version/help/unknown behavior and symlink
  invocation.

### Subtask: repair already-closed direct command routes

Current status: completed.

Remaining parts: none.

Implementation details:

- Direct `classify`, `plan`, `verify`, and `reflect` routes invoke their native
  Python modules.
- `apply` remains available through its live sibling entrypoint.
- `execute` intentionally remains the one live Bash command fork.

### Subtask: close CLI runtime seams

Current status: completed.

Remaining parts: none.

Implementation details:

- Public-path scheduler, spawn, sandbox, web, CI, installer, and research
  callers remain valid without churn because the argv contract is unchanged.
- Deadline, config snapshot, repository integrity, rubric scoring, and reward
  grading now call native Python ports; best-effort and stdout contracts are
  preserved explicitly.

### Subtask: satisfy migration gates

Current status: completed.

Remaining parts: none.

Evidence:

- Durable pre-retirement evidence remains green in the run directory.
- A third audit detected that the original helper could delegate back to
  Python. Corrected true-Bash evidence now proves 40 legacy dispatcher
  assertions plus exact version/help/doctor/error parity.
- 8 standalone CLI tests and 46 dispatcher assertions pass.
- Focused Pyright reports zero errors.
- Post-retirement parity, CLI feature acceptance, the 48-row ledger, and
  CLI-specialized fork closure pass.
- Three source-requirements audits are recorded under `docs/todos/`; the third
  corrected the runtime-selection flaw in the original Bash oracle before
  final promotion.
