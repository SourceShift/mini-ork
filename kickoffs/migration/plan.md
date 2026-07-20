# Close the `plan` integration fork

Status: prepared on 2026-07-20; paid self-migrate execution not started.

## Goal

Make `mini_ork/ported/mini_ork_plan.py` the sole plan runtime, replace its
remaining Bash `llm-dispatch.sh` subprocess with the native Python dispatcher,
repoint every executable caller away from `bin/mini-ork-plan`, and retire the
Bash entrypoint as a reviewable proposal. Preserve stdout, stderr, exit codes,
dry-run behavior, schema validation, repair limits, profile gates, given-plan
behavior, DB/trace writes, output paths, and provider/cost telemetry.

The recipe must edit only the explicit isolated target. It must not edit or
commit in the source checkout.

## Fork

- **fork:** `plan`
- **isolated target:** `/private/tmp/mini-ork-self-migrate-plan`
- **baseline:** `928db915`
- **python entrypoint:** `/private/tmp/mini-ork-self-migrate-plan/mini_ork/ported/mini_ork_plan.py`
- **bash entrypoint to retire:** `/private/tmp/mini-ork-self-migrate-plan/bin/mini-ork-plan`

## Preflight evidence

The exact isolated target is clean and has this no-cost baseline:

- `tests/unit/test_mini_ork_plan_py.py`: 9 passed.
- `tests/integration/test_bin_plan.sh`: 10 assertions passed.
- `tests/integration/test_given_plan.sh`: 7 assertions passed.
- `gates/feature_acceptance.sh plan`: pass.
- Focused Pyright currently reports three baseline errors in
  `mini_ork/ported/mini_ork_plan.py` at the float conversion near line 419 and
  the optional dispatch call near line 569. The migration must leave focused
  Pyright clean; do not waive or hide these errors.

The recipe's pre-retirement node must capture its own durable green report
before the Bash entrypoint can be removed. The workflow-phase-aware hollow-run
guard allows that baseline verifier to run before final artifacts exist; later
verifiers remain fail-closed.

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
run-local executable adapter pattern proven by the reflect and classify
closures against the authenticated Anthropic-compatible messages endpoint.

## Outbound seam to close first

`mini_ork/ported/mini_ork_plan.py::_default_llm_dispatch` currently executes
`bash -c`, sources `lib/llm-dispatch.sh`, and calls the Bash `llm_dispatch`
function. This makes the Python port non-native and blocks Bash retirement.

Replace that subprocess with the existing native
`mini_ork.ported.llm_dispatch.llm_dispatch` API. Preserve the injectable
`dispatch(task_class, node_type, prompt) -> (returncode, combined_output)`
contract by capturing native stdout and stderr into the same buffer. Preserve
provider selection, retry/fuse behavior, cost and duration sidecars,
`llm_calls` telemetry, protocol-block stripping, and failure return codes.

## Runtime references to resolve

The current executable-tree scan found these literal or dynamic dependencies:

- `bin/mini-ork` — direct subcommand dispatch and normal run lifecycle plan
  stage.
- `mini_ork/ported/mini_ork_cli.py` — `_EXEC_SUBS` direct dispatch and dynamic
  `_bin(root, "plan")` lifecycle call.
- `scripts/runtime-parity-harness.sh` — direct Bash plan parity calls.
- `tests/unit/test_mini_ork_plan_py.py` — live Bash parity oracle.
- `tests/integration/test_bin_plan.sh`
- `tests/integration/test_given_plan.sh`
- `tests/e2e/test_e2e_recipe_bdd_first.sh`
- `tests/e2e/test_e2e_recipe_code_fix.sh`
- `tests/security/test_sec_hooks_attack_surface.sh`
- `tests/security/test_sec_kickoff_command_injection.sh`
- `tests/security/test_sec_oversized_input.sh`
- `tests/test_web_smoke.py` — reads the Bash source directly.

Also refresh current runtime descriptions in `bin/mini-ork-execute`,
`mini_ork/ported/mini_ork_execute.py`, `bin/mini-ork-self-improve`,
`lib/llm-dispatch.sh`, `lib/context_assembler.sh`,
`lib/active_state_index.sh`, `lib/extract_verdict.py`,
`mini_ork/web/routes/run_detail.py`, and
`scripts/slm_plan_or_fallback.sh`. Historical audits and plans may retain path
mentions when they clearly describe past state.

## Required implementation contracts

1. Make the Python planner runtime-native before deleting the Bash entrypoint.
2. Replace every executable call to `bin/mini-ork-plan` with the Python module
   using an explicit `PYTHONPATH`/module environment rooted at the target.
3. Preserve the planner dispatch callable's combined output and return-code
   contract while using the native Python LLM dispatcher.
4. Preserve plan JSON normalization, verifier-contract enforcement, repair
   exhaustion, deterministic fallback opt-in, profile blocking, dry-run,
   `MO_GIVEN_PLAN`, explicit task-class/output flags, DB writes, trace writes,
   and stdout/stderr discipline.
5. Convert the unit suite from a live Bash oracle to standalone golden and
   behavioral contracts only after the durable pre-retirement report is green.
6. Keep integration, E2E, security, web, and runtime-parity coverage attached
   to the Python-sole entrypoint; do not delete tests because Bash is gone.
7. Resolve the three focused Pyright errors and delete `bin/mini-ork-plan` only
   after all callers and tests are repointed.

## Acceptance criteria

- `verifiers/pre-retirement-parity.sh` records the green Bash/Python oracle
  before retirement.
- `verifiers/parity.sh` validates the post-retirement standalone plan contract.
- `verifiers/feature-acceptance.sh` runs plan feature acceptance, unit,
  integration, given-plan, relevant security, and focused Pyright coverage.
- `verifiers/ledger-shape.sh` classifies every behavior in
  `mini_ork_plan.py`; every agentic row includes a concrete cost or
  verifiability opportunity.
- `verifiers/fork-closure.sh` confirms `bin/mini-ork-plan` is absent and no
  literal or dynamic runtime reference survives in executable trees.
- Reviewer inputs include all five reports, the diff, integration map, ledger,
  and detailed verdict. The detailed `verdict.json` says `"pass": true` before
  promotion.
- The source checkout receives only the reviewed, passing proposal and focused
  migration-documentation updates.

## Verification commands

- `python3 -m pytest tests/unit/test_mini_ork_plan_py.py -q -p no:cacheprovider`
- `python3 -m pytest tests/unit/test_mini_ork_cli_py.py -q -p no:cacheprovider`
- `bash tests/integration/test_bin_plan.sh`
- `bash tests/integration/test_given_plan.sh`
- `bash gates/feature_acceptance.sh plan`
- `python3 -m pyright mini_ork/ported/mini_ork_plan.py mini_ork/ported/mini_ork_cli.py`
- relevant plan E2E and security scripts listed above
- `bash recipes/self-migrate/verifiers/fork-closure.sh` with the run environment populated
- `git diff --check`

## Files in scope

All paths below are rooted at `/private/tmp/mini-ork-self-migrate-plan`:

- `bin/mini-ork-plan`
- `bin/mini-ork`
- `mini_ork/ported/mini_ork_plan.py`
- `mini_ork/ported/mini_ork_cli.py`
- `scripts/runtime-parity-harness.sh`
- `gates/feature_acceptance.sh`
- `recipes/self-migrate/verifiers/parity.sh`
- `recipes/self-migrate/verifiers/feature-acceptance.sh`
- `tests/unit/test_mini_ork_plan_py.py`
- `tests/unit/test_mini_ork_cli_py.py`
- `tests/integration/test_bin_plan.sh`
- `tests/integration/test_given_plan.sh`
- `tests/e2e/test_e2e_recipe_bdd_first.sh`
- `tests/e2e/test_e2e_recipe_code_fix.sh`
- `tests/security/test_sec_hooks_attack_surface.sh`
- `tests/security/test_sec_kickoff_command_injection.sh`
- `tests/security/test_sec_oversized_input.sh`
- `tests/test_web_smoke.py`
- current public runtime descriptions listed above

The seam mapper may add a missing executable caller to scope when it provides an
exact path and contract. It must not broaden scope into unrelated features.

## Rollback

The recipe is propose-not-commit. If any verifier or reviewer fails, preserve
run evidence, reject the proposal, and leave the source branch at the committed
classify closure. Never trigger a paid retry or repair loop without explicit
approval.
