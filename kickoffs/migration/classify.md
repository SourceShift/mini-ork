# Close the `classify` integration fork

Status: completed and source-applied on 2026-07-20 from the passing proposal
produced by `run-1784528328-42404`.

## Goal

Make `mini_ork/ported/mini_ork_classify.py` the sole classify runtime, repoint
every executable caller away from `bin/mini-ork-classify`, and retire the Bash
entrypoint as a reviewable proposal. Preserve stdout, exit codes, dry-run
behavior, DB/trace writes, workflow-version overrides, kickoff size limits,
and hostile-input handling.

The recipe must edit only the explicit isolated target. It must not edit or
commit in the source checkout.

## Fork

- **fork:** `classify`
- **isolated target:** `/private/tmp/mini-ork-self-migrate-classify`
- **baseline:** `86eba5e7`
- **python entrypoint:** `/private/tmp/mini-ork-self-migrate-classify/mini_ork/ported/mini_ork_classify.py`
- **bash entrypoint to retire:** `/private/tmp/mini-ork-self-migrate-classify/bin/mini-ork-classify`

## Preflight evidence

The exact isolated target is clean and passes the live pre-retirement oracle:

- `tests/unit/test_mini_ork_classify_py.py`: 5 passed.
- `tests/integration/test_bin_classify.sh`: 9 assertions passed.
- `gates/feature_acceptance.sh classify`: pass.
- Pyright on classify and the Python CLI: 0 errors.

The recipe's pre-retirement node must capture its own durable green report
before the Bash entrypoint can be removed. The workflow-phase-aware hollow-run
guard allows that baseline verifier to run before final artifacts exist; later
verifiers remain fail-closed.

## Completion evidence

- The run used Kimi for planning, Codex for implementation, and GLM 5.2 for
  seam mapping, the authoritative ledger, and review. MiniMax was not selected.
- The durable pre-retirement oracle passed before the Bash entrypoint was
  removed. Post-retirement parity, feature acceptance, the 29-row ledger, and
  deterministic fork closure also pass.
- The proposal deletes `bin/mini-ork-classify`, preserves the classify stdout,
  exit-code, dry-run, DB, trace, override, size-limit, and hostile-input
  contracts, and repoints all mapped runtime and test callers to the Python
  module.
- The detailed `verdict.json`, GLM reviewer, and run-level workflow verdict
  pass. The outer command returned non-zero only because the generic Python
  verifier passed incomplete context to globally registered oracle gates;
  their `defer` results were aggregated as a failure. No paid replay was used.
- The reviewed proposal applied cleanly to the source checkout and was replayed
  with focused unit, integration, security, E2E, Pyright, migration-gate, and
  diff-hygiene checks.

## Provider policy

Use only these provider values:

- Kimi: planner and broad research.
- Codex: migrator and coding fallback.
- GLM 5.2: seam map, authoritative ledger, reviewer, and judgment fallback.

Load Kimi and GLM credentials process-locally from
`/Users/admin/ps/scripts/cl_kimi.sh` and `cl_glm.sh`, translating their
Claude-compatible tokens into the run-local provider variables. Clear gateway
variables before Codex dispatch. Never persist secret values in the repository,
runtime configuration, kickoff, or run artifacts. Do not route any role to
MiniMax or Opus.

Kimi's repository Claude wrapper advertises a stale model name. Use the same
run-local executable adapter pattern proven by the reflect closure against the
authenticated Anthropic-compatible messages endpoint.

## Runtime references to resolve

The current executable-tree scan found these literal or dynamic dependencies:

- `bin/mini-ork` — direct subcommand dispatch, user-first probe, and normal run
  lifecycle classify stage.
- `bin/mini-ork-validate` — kickoff-to-recipe resolution.
- `lib/llm-dispatch.sh` — provider/task classification helper.
- `mini_ork/ported/mini_ork_cli.py` — two dynamic `_bin(root, "classify")`
  subprocess calls.
- `mini_ork/web/routes/run_detail.py` — web run-detail classify invocation.
- `tests/e2e/test_e2e_recipe_bdd_first.sh`
- `tests/e2e/test_e2e_recipe_code_fix.sh`
- `tests/e2e/test_e2e_trace_lifecycle.sh`
- `tests/integration/test_bin_classify.sh`
- `tests/security/test_sec_env_var_pollution.sh`
- `tests/security/test_sec_hooks_attack_surface.sh`
- `tests/security/test_sec_kickoff_command_injection.sh`
- `tests/security/test_sec_kickoff_path_traversal.sh`
- `tests/security/test_sec_malformed_yaml.sh`
- `tests/security/test_sec_oversized_input.sh`
- `tests/security/test_sec_sql_injection_run_id.sh`
- `tests/unit/test_mini_ork_classify_py.py`

Also update current public documentation and Python module descriptions that
still identify the deleted Bash file as the runtime. Historical audits and
plans may retain path mentions when they clearly describe past state; runtime
closure is enforced only across executable trees.

## Required implementation contracts

1. Replace every executable call to `bin/mini-ork-classify` with the Python
   module using an explicit `PYTHONPATH`/module environment rooted at the target.
2. Preserve stdout exactly where callers parse `task_class=...` and
   `workflow_version=...`; do not introduce logging on stdout.
3. Preserve dry-run as side-effect free and retain non-dry DB/trace writes,
   explicit task-class override, workflow-version override, missing-file exit
   behavior, and `MO_MAX_KICKOFF_BYTES` enforcement.
4. Convert the unit suite from a live Bash oracle to standalone golden and
   behavioral contracts only after the durable pre-retirement report is green.
5. Keep integration, E2E, web-route, validation, and security coverage attached
   to the Python-sole entrypoint; do not delete tests merely because the Bash
   path is gone.
6. Delete `bin/mini-ork-classify` only after all callers and tests are repointed.

## Acceptance criteria

- `verifiers/pre-retirement-parity.sh` records the green five-test Bash/Python
  oracle before retirement.
- `verifiers/parity.sh` validates the post-retirement classify contract.
- `verifiers/feature-acceptance.sh` runs classify feature acceptance, the unit
  contracts, integration coverage, relevant security coverage, and Pyright on
  classify plus changed Python callers.
- `verifiers/ledger-shape.sh` classifies every behavior in
  `mini_ork_classify.py`; every agentic row includes a concrete cost or
  verifiability opportunity.
- `verifiers/fork-closure.sh` confirms `bin/mini-ork-classify` is absent and no
  literal or dynamic runtime reference survives in executable trees.
- Reviewer inputs include all five reports, the diff, integration map, ledger,
  and detailed verdict. The detailed `verdict.json` says `"pass": true` before
  promotion.
- The source checkout receives only the reviewed, passing proposal and focused
  migration-documentation updates.

## Verification commands

- `python3 -m pytest tests/unit/test_mini_ork_classify_py.py -q -p no:cacheprovider`
- `bash tests/integration/test_bin_classify.sh`
- `bash gates/feature_acceptance.sh classify`
- `python3 -m pyright mini_ork/ported/mini_ork_classify.py mini_ork/ported/mini_ork_cli.py mini_ork/web/routes/run_detail.py`
- relevant `tests/security/test_sec_*.sh` scripts listed above
- relevant classify E2E scripts listed above
- `bash recipes/self-migrate/verifiers/fork-closure.sh` with the run environment populated
- `git diff --check`

## Files in scope

All paths below are rooted at `/private/tmp/mini-ork-self-migrate-classify`:

- `bin/mini-ork-classify`
- `bin/mini-ork`
- `bin/mini-ork-validate`
- `lib/llm-dispatch.sh`
- `mini_ork/ported/mini_ork_classify.py`
- `mini_ork/ported/mini_ork_cli.py`
- `mini_ork/web/routes/run_detail.py`
- `scripts/runtime-parity-harness.sh`
- `gates/feature_acceptance.sh`
- `recipes/self-migrate/verifiers/parity.sh`
- `recipes/self-migrate/verifiers/feature-acceptance.sh`
- `tests/unit/test_mini_ork_classify_py.py`
- `tests/integration/test_bin_classify.sh`
- `tests/e2e/test_e2e_recipe_bdd_first.sh`
- `tests/e2e/test_e2e_recipe_code_fix.sh`
- `tests/e2e/test_e2e_trace_lifecycle.sh`
- `tests/security/test_sec_env_var_pollution.sh`
- `tests/security/test_sec_hooks_attack_surface.sh`
- `tests/security/test_sec_kickoff_command_injection.sh`
- `tests/security/test_sec_kickoff_path_traversal.sh`
- `tests/security/test_sec_malformed_yaml.sh`
- `tests/security/test_sec_oversized_input.sh`
- `tests/security/test_sec_sql_injection_run_id.sh`
- current public docs that describe the classify runtime

The seam mapper may add a missing executable caller to scope when it provides an
exact path and contract. It must not broaden scope into unrelated features.

## Rollback

The recipe is propose-not-commit. If any verifier or reviewer fails, preserve
run evidence, reject the proposal, and leave the source branch at the committed
reflect closure. Never trigger a paid retry or repair loop without explicit
approval.
