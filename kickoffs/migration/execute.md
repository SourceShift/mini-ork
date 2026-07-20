# Close the `execute` integration fork

Status: ready for one authorized Kimi/Codex/GLM self-migrate run on 2026-07-20.

## Goal

Make `mini_ork/ported/mini_ork_execute.py` the sole executor implementation,
route the top-level Python CLI to it in-process, repoint every executable
caller, and retire `bin/mini-ork-execute`. Preserve the complete node lifecycle,
provider routing, verification, learning, publisher, rollback, checkpoint,
telemetry, concurrency, and failure contracts as one reviewable migration.

## Fork

- **fork:** `execute`
- **isolated target:** `/private/tmp/mini-ork-self-migrate-execute`
- **python implementation:** `/private/tmp/mini-ork-self-migrate-execute/mini_ork/ported/mini_ork_execute.py`
- **Bash entrypoint to retire:** `/private/tmp/mini-ork-self-migrate-execute/bin/mini-ork-execute`

## Required true-Bash oracle

The pre-retirement oracle must execute the untouched Bash implementation with
`MINI_ORK_RUNTIME=bash`. Do not accept evidence that reaches Python through
`lib/runtime-select.sh`, and do not treat helper extraction alone as proof of
the public executor contract. Preserve the durable oracle evidence after the
Bash entrypoint is deleted.

## Outbound seams to close

The Python executor must use native Python ports for every Bash-owned library
bridge in its live orchestration path:

- `lib/llm-dispatch.sh` -> `mini_ork.ported.llm_dispatch`
- `lib/lane-helpers.sh` capability checks -> `mini_ork.ported.lane_helpers`
- `lib/context_assembler.sh` learned failure modes and operator steering ->
  native context modules; preserve opt-out, empty-result, and best-effort rules
- `lib/intervention_gate.sh` -> a native Python gate with the same proceed/block
  return contract

Executable verifier references may remain scripts because recipe verifiers are
user-defined executable contracts, not an implementation fallback. Git and
provider subprocesses may remain where they are the external boundary. Any
other sourced Bash library found in the live executor path must be classified
and either ported or justified as an external executable contract.

## Inbound references to resolve

- `mini_ork/ported/mini_ork_cli.py`: remove `execute` from `_EXEC_SUBS`, route
  direct execute and the run lifecycle to `mini_ork_execute.main` in-process,
  and preserve captured stdout/stderr plus exit status.
- `lib/runtime-select.sh` and `scripts/runtime-parity-harness.sh`: remove the
  retired execute fallback and replace historical parity with a standalone
  Python/golden contract after durable Bash evidence is captured.
- Source-based learning and smoke scripts (`learning-loop-live-validate.sh`,
  `rlm-shared-brain-smoke.sh`, `smoke-learning-loops.sh`) must import or call
  native Python helpers rather than source the deleted entrypoint.
- Integration, unit, E2E, performance, observability, security, scheduler,
  web, gate, and recipe verifier callers discovered by the seam map must use
  `python3 -m mini_ork.ported.mini_ork_execute`, the public `mini-ork execute`
  route, or direct native helpers as appropriate.
- Comments and current operator/migration docs must not describe the retired
  Bash executor as live. Historical evidence may name it when clearly marked.

## Acceptance criteria

- A durable, explicit-`MINI_ORK_RUNTIME=bash` pre-retirement oracle is green.
- `bin/mini-ork-execute` is physically absent.
- `python3 -m mini_ork.ported.mini_ork_execute --help`, missing-plan errors,
  dry-run, node filtering, dispatch-mode overrides, and unknown flags preserve
  the public observable contract.
- Direct `mini-ork execute` and the full `mini-ork run` lifecycle call the
  native executor without resolving a sibling Bash entrypoint.
- No executable/runtime reference to `bin/mini-ork-execute`, dynamic
  `_bin(..., "execute")`, or `source ... mini-ork-execute` survives.
- Native dispatch, context, capability, intervention, verification, learning,
  publisher, rollback, checkpoint, telemetry, and bounded-parallel contracts
  have focused tests.
- Execute feature acceptance, focused Pyright, the static-feature ledger, and
  deterministic fork closure all pass.
- No credential value, provider adapter, temporary home, run artifact, local
  state database, or user-owned `.mini-ork/config/agents.yaml` enters a commit.

## Verification commands

- `python3 -m pytest tests/unit/test_mini_ork_execute_py.py -q -p no:cacheprovider`
- `bash tests/integration/test_bin_execute.sh`
- `python3 -m pyright mini_ork/ported/mini_ork_execute.py mini_ork/ported/mini_ork_cli.py`
- `MO_FORK=execute bash recipes/self-migrate/verifiers/parity.sh`
- `MO_FORK=execute bash recipes/self-migrate/verifiers/feature-acceptance.sh`
- `MO_FORK=execute bash recipes/self-migrate/verifiers/ledger-shape.sh`
- `MO_FORK=execute bash recipes/self-migrate/verifiers/fork-closure.sh`

## Provider policy

- Kimi: planner and broad discovery.
- Codex: migrator/implementer.
- GLM 5.2: seam map, static-feature ledger, and reviewer.
- MiniMax and DeepSeek are forbidden.
- Load Kimi and GLM credentials process-locally from
  `/Users/admin/ps/scripts/cl_kimi.sh` and `cl_glm.sh`; never persist values.
- The user authorized exactly one paid execute migration run. Do not trigger a
  paid retry or repair run without new approval.

## Files in scope

- `mini_ork/ported/mini_ork_execute.py`
- `mini_ork/ported/mini_ork_cli.py`
- `bin/mini-ork-execute` (delete)
- Native modules needed to close the four outbound Bash-library seams
- Tests and scripts that execute or source the retired entrypoint
- `lib/runtime-select.sh`, `scripts/runtime-parity-harness.sh`,
  `gates/feature_acceptance.sh`, and self-migrate verifiers
- Current execute/operator/migration documentation and completion-audit files

The seam mapper must expand this list from the repository. A file is in scope
only when it closes an execute runtime edge, preserves its observable contract,
or records migration evidence; unrelated cleanup is forbidden.
