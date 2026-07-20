# Execute migration completion audit

Status: in progress

Last worked on: 2026-07-20

## Task: close the final execute fork

Current status: isolated target, explicit migration contract, and durable
pre-retirement evidence are prepared; the authorized Kimi/Codex/GLM run has
not started.

### Subtask: capture a real Bash oracle

Current status: completed for the pre-migration target.

Remaining parts: preserve and cite the evidence after entrypoint retirement.

Evidence:

- `MINI_ORK_RUNTIME=bash python3 -m pytest tests/unit/test_mini_ork_execute_py.py -q -p no:cacheprovider`: 51 passed.
- `MINI_ORK_RUNTIME=bash bash tests/integration/test_bin_execute.sh`: 10 assertions passed.
- `python3 -m pyright mini_ork/ported/mini_ork_execute.py mini_ork/ported/mini_ork_cli.py`: 0 errors.
- Durable logs are outside the repository at `/private/tmp/mini-ork-execute-preflight/`.

### Subtask: close outbound Bash-library seams

Current status: not started.

Remaining parts: use native dispatch, capability, learned-context, and
intervention-gate implementations while preserving fail-open/fail-closed and
stdout contracts.

### Subtask: repoint every inbound runtime edge

Current status: not started.

Remaining parts: route the Python CLI in-process, convert source-based scripts
to native imports, update executable tests and gates, and delete the Bash
entrypoint only when deterministic closure is possible.

### Subtask: satisfy migration and product gates

Current status: not started.

Remaining parts: replay focused tests, Pyright, execute feature acceptance,
the five self-migrate reports, two requirements audits, secret/scope checks,
and clean-main promotion before pushing.
