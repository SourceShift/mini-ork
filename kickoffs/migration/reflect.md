# Close the `reflect` integration fork

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
`MINIMAX_API_KEY` is unavailable. The isolated target remained clean. For the
retry, preserve the user-owned policy file and apply process-local lane-cache
overrides instead:

```bash
export _MO_LANE_PLANNER=codex
export _MO_LANE_GLM_LENS=codex
```

This keeps the authoritative `opus_lens` mapper, ledger, and reviewer lanes and
the `codex_lens` migrator unchanged. A retry still requires explicit cost
approval.

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
  Bash-file implementation check with a Python-port capability check.

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
- `/private/tmp/mini-ork-self-migrate-reflect/scripts/runtime-parity-harness.sh`
- `/private/tmp/mini-ork-self-migrate-reflect/gates/feature_acceptance.sh`
- `/private/tmp/mini-ork-self-migrate-reflect/recipes/audit-findings-validator/verifiers/missing-impl.sh`

## Rollback

The recipe is propose-not-commit. If any verifier or reviewer fails, retain the
run artifacts, reject the diff, and keep the source checkout on the committed
verify closure. Never delete or repoint the Bash entrypoint without a passing
detailed verdict and a reviewed diff.
