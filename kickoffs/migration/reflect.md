# Close the `reflect` integration fork

Status: completed locally from `run-1784503045-70610` on 2026-07-20.

## Goal

Close the `reflect` fork: make
`mini_ork/ported/mini_ork_reflect.py` the sole implementation, repoint every
runtime caller away from `bin/mini-ork-reflect`, and retire the Bash entrypoint
as a reviewable diff. Do not edit the source checkout directly; the recipe
must operate on the explicit isolated target.

## Fork

- **fork:** `reflect`
- **isolated target:** `/private/tmp/mini-ork-self-migrate-reflect`
- **python entrypoint:** `/private/tmp/mini-ork-self-migrate-reflect/mini_ork/ported/mini_ork_reflect.py`
- **bash entrypoint to retire:** `/private/tmp/mini-ork-self-migrate-reflect/bin/mini-ork-reflect`

## Preflight evidence

The exact isolated target passed all 8 tests in
`tests/unit/test_mini_ork_reflect_py.py` before retirement. Two reflect tests
failed in the dirty source checkout during the repository-wide suite, but they
do not reproduce in this clean worktree; do not modify them unless the isolated
run produces a new failure. The recipe's `pre-retirement-parity` node remains
fail-closed and must preserve its own durable green report before migration.

### Provider preflight

The first paid launch (`run-1784502357-9667`) stopped before planning because
the frozen operator policy routes `planner` and `glm_lens` to MiniMax while
`MINIMAX_API_KEY` is unavailable. The isolated target remained clean.

The retry uses the dedicated runtime home
`/private/tmp/mini-ork-self-migrate-reflect-home`. Its frozen policy contains
only three provider values:

- Kimi: planner and general research
- Codex: migrator and coding fallback
- GLM: seam mapper, authoritative ledger, reviewer, and judgment fallback

Load the Kimi and GLM credentials process-locally from
`/Users/admin/ps/scripts/cl_kimi.sh` and `cl_glm.sh`, translate their
Claude-compatible tokens into `KIMI_API_KEY` and `GLM_API_KEY`, then clear the
temporary Anthropic gateway variables before launching. Never copy secret
values into this kickoff, the runtime home, or run artifacts.

The successful run used GLM model `glm-5.2`. Kimi's authenticated catalog
exposed `kimi-for-coding`, while the repository Claude wrapper's hard-coded
model was rejected; a mode-700 run-local executable adapter called the
Anthropic-compatible messages endpoint directly. No credentials were persisted.

## Inbound references to resolve

- `bin/mini-ork` — repoint direct and automatic run-lifecycle reflection.
- `bin/mini-ork-execute` — repoint the legacy executor's automatic reflection.
- `mini_ork/ported/mini_ork_execute.py` — replace its direct Bash subprocess.
- `mini_ork/ported/mini_ork_cli.py` — replace dynamic `_bin(root, "reflect")`
  dispatch with the native module.
- `tests/test_gepa_wiring_py.py` — remove direct dependence on the Bash CLI.
- `tests/integration/test_bin_reflect.sh` — convert integration coverage to the
  Python-sole entrypoint contract.
- `tests/unit/test_mini_ork_reflect_py.py` — preserve pre-retirement parity
  evidence, then convert the suite to Python golden/behavioral contracts.
- `scripts/runtime-parity-harness.sh` — add a focused post-retirement `reflect`
  contract if the global harness cannot run without the removed Bash oracle.
- `gates/feature_acceptance.sh` — retain the Python-sole reflect probe.

The deterministic closure gate also searches executable/runtime trees. Update
the following stale path references without changing their behavior:

- `bin/mini-ork-bug-collector`
- `lib/reflection_pipeline.sh`
- `mini_ork/ported/mini_ork_bug_collector.py`
- `mini_ork/optimize/gepa.py`
- `mini_ork/ported/mini_ork_reflect.py`
- `recipes/audit-findings-validator/verifiers/missing-impl.sh` — replace its
  Bash-file implementation check with a Python-port capability check. The seam
  map proved this path is a phantom edge in the isolated target, so no file was
  created or changed.

## Acceptance criteria

- `verifiers/pre-retirement-parity.sh` captures a green, durable Bash/Python
  parity report before the Bash entrypoint can be deleted.
- `verifiers/parity.sh` validates the post-change reflect contract.
- `verifiers/feature-acceptance.sh` passes `gates/feature_acceptance.sh reflect`,
  the reflect unit suite, integration coverage, and Pyright on the port.
- `verifiers/ledger-shape.sh` classifies every behavior in
  `mini_ork_reflect.py`; each agentic row has a concrete cost/verifiability
  opportunity.
- `verifiers/fork-closure.sh` confirms `bin/mini-ork-reflect` is absent and no
  literal or dynamic runtime reference survives.
- The run mirror is harvested before review, the reviewer receives all five
  recipe reports plus the diff/map/ledger/detailed verdict, and generic status
  remains in `run-verdict.json`.
- The detailed `verdict.json` says `"pass": true` before any proposal is
  promoted to the source branch.

## Verification commands

- `python3 -m pytest tests/unit/test_mini_ork_reflect_py.py -q -p no:cacheprovider`
- `python3 -m pytest tests/test_gepa_wiring_py.py -q -p no:cacheprovider`
- `bash tests/integration/test_bin_reflect.sh`
- `bash gates/feature_acceptance.sh reflect`
- `python3 -m pyright mini_ork/ported/mini_ork_reflect.py mini_ork/ported/mini_ork_cli.py mini_ork/ported/mini_ork_execute.py`
- `bash recipes/self-migrate/verifiers/fork-closure.sh` with the run environment populated
- `git diff --check`

## Files in scope

- `/private/tmp/mini-ork-self-migrate-reflect/bin/mini-ork-reflect`
- `/private/tmp/mini-ork-self-migrate-reflect/bin/mini-ork`
- `/private/tmp/mini-ork-self-migrate-reflect/bin/mini-ork-execute`
- `/private/tmp/mini-ork-self-migrate-reflect/bin/mini-ork-bug-collector`
- `/private/tmp/mini-ork-self-migrate-reflect/lib/reflection_pipeline.sh`
- `/private/tmp/mini-ork-self-migrate-reflect/mini_ork/ported/mini_ork_reflect.py`
- `/private/tmp/mini-ork-self-migrate-reflect/mini_ork/ported/mini_ork_execute.py`
- `/private/tmp/mini-ork-self-migrate-reflect/mini_ork/ported/mini_ork_cli.py`
- `/private/tmp/mini-ork-self-migrate-reflect/mini_ork/ported/mini_ork_bug_collector.py`
- `/private/tmp/mini-ork-self-migrate-reflect/mini_ork/optimize/gepa.py`
- `/private/tmp/mini-ork-self-migrate-reflect/tests/test_gepa_wiring_py.py`
- `/private/tmp/mini-ork-self-migrate-reflect/tests/integration/test_bin_reflect.sh`
- `/private/tmp/mini-ork-self-migrate-reflect/tests/unit/test_mini_ork_reflect_py.py`
- `/private/tmp/mini-ork-self-migrate-reflect/tests/unit/test_mini_ork_execute_py.py`
- `/private/tmp/mini-ork-self-migrate-reflect/scripts/runtime-parity-harness.sh`
- `/private/tmp/mini-ork-self-migrate-reflect/gates/feature_acceptance.sh`
- `/private/tmp/mini-ork-self-migrate-reflect/docs/FEATURES.md`
- `/private/tmp/mini-ork-self-migrate-reflect/docs/LEARNING-LOOP-LIFECYCLE.md`
- `/private/tmp/mini-ork-self-migrate-reflect/docs/architecture/coevolve-ecosystem.md`
- `/private/tmp/mini-ork-self-migrate-reflect/docs/architecture/techniques-compendium.md`

## Completion evidence

- Detailed migration verdict: pass.
- Five reports: pre-retirement parity, post-retirement parity, feature
  acceptance, 27-row ledger shape, and fork closure all pass.
- Reviewer: pass; rubric: 7/8.
- Source verification: 11 reflect/GEPA tests, 11 integration assertions, 8
  focused parity cases, reflect feature acceptance, 57 executor/CLI tests,
  Pyright with zero errors, Bash syntax, and `git diff --check` all pass.
- Completion-audit repair: reviewer inputs now include the standalone
  pre-retirement report, and workflow-phase-aware artifact guarding prevents a
  pre-implementation verifier from creating a false run failure.

## Rollback

The recipe is propose-not-commit. If any verifier or reviewer fails, retain the
run artifacts, reject the diff, and keep the source checkout on the committed
verify closure. Never delete or repoint the Bash entrypoint without a passing
detailed verdict and a reviewed diff.
