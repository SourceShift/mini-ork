# Complete Remaining Bash-to-Python Migration — Agent Handoff

Status: active after `origin/main` commit `0bfdc3f6` on 2026-07-20.

This is the master handoff for the **whole remaining migration program**. The
dispatcher work is only one track. Its detailed sub-handoff is
`docs/migration/remaining-dispatcher-fixture-handoff.md`.

## Mission

Finish ownership migration from legacy Bash implementations and fixtures to
the supported Python runtime while preserving every product, operator,
integration, security, observability, learning, and public-CLI contract.

The work is complete only when every module has an explicit owner and terminal
decision, every retired Bash library has zero executable/test/fixture/benchmark
dependents, the complete validation stack passes, and the final commits are on
`origin/main`.

## Start here

Read these documents before discovery or edits:

1. `AGENTS.md`
2. `docs/migration/remaining-migration-handoff.md`
3. `docs/migration/ported-module-ownership-recursive-plan.md`
4. `docs/migration/self-migrate-feature-manifest.md`
5. `docs/migration/remaining-dispatcher-fixture-handoff.md`
6. the task-, architecture-, operator-, and test-related documents for the
   selected ownership unit

The historical Phase 0 report covered 126 ported modules at `c7ccc512`. It is
not a current deletion authorization. Refresh the live inventory from current
`origin/main` before selecting each new unit.

## Current promoted state

The top-level runtime forks are complete:

- verify;
- reflect and gradient retirement;
- classify;
- plan;
- CLI;
- execute;
- scheduler;
- context assembler and similarity ownership;
- profile answerer;
- invoke-prompt;
- comparative opinions;
- pre-push review entrypoint and runtime consolidation.

Recent lower-level dispatcher-related commits already on `main`:

- `91861459` — native executable transcript writer;
- `5cd816ae` — native retry fixture and Bash retry-test retirement;
- `24220991` — native secret-redaction fixture and Bash-test retirement;
- `ff74a68f` — native `llm_calls` ledger fixture and Bash-test retirement;
- `be2fa515` — initial native tool-grant contracts;
- `0bfdc3f6` — dispatcher sub-handoff.

Do not repeat these completed units. Revalidate them only when an adjacent
change can affect their contracts.

## Migration model

One queue item is one ownership seam. Each unit follows:

```text
refresh inventory
  -> reconstruct history and requirements
  -> map all callers/tests/fixtures
  -> capture old behavior
  -> implement one canonical owner
  -> rewire all in-scope callers
  -> replace obsolete fixtures
  -> prove sole ownership
  -> audit requirements twice
  -> commit, merge, push
  -> refresh inventory again
```

Never migrate the entire `mini_ork/ported/` directory mechanically. A port may
be active, duplicated, dormant, dead, or an intentional external adapter.

## Complete remaining workstreams

The exact file queue must be regenerated, but none of these workstreams may be
omitted.

### A. Dispatcher, provider, telemetry, and tool-grant closure

Canonical owners:

- `mini_ork.dispatch.*`
- `mini_ork.dispatch.llm_dispatch`
- `mini_ork.dispatch.transcripts`

Remaining work includes provider registry/wrapper fixtures, live-provider
boundaries, tool-grant source-shape and subprocess coverage, Codex usage/cost
sidecars, duration capture, run artifacts, execute-gate fixture splitting, and
the final retirement of `lib/llm-dispatch.sh`.

Follow `docs/migration/remaining-dispatcher-fixture-handoff.md` for the exact
subqueue and its validation rules.

### B. Gate framework and safety gates

Known Bash/Python ownership pairs include:

- `lib/gate_bootstrap.sh` / `mini_ork.gates.gate_bootstrap`
- `lib/gate_registry.sh` / `mini_ork.gates.gate_registry`
- `lib/gates_common.sh` / `mini_ork.gates.common`
- `lib/coalition_gate.sh` / `mini_ork.gates.coalition_gate`
- `lib/circuit_breaker.sh` / `mini_ork.recovery.circuit_breaker`
- `lib/adaptive_stability.sh` / `mini_ork.gates.adaptive_stability`
- `lib/cw_por.sh` / `mini_ork.gates.cw_por`
- `lib/honest_ci_gate.sh` / `mini_ork.gates.honest_ci_gate`
- `lib/krippendorff_alpha_gate.sh` / `mini_ork.gates.krippendorff_alpha_gate`
- `lib/citation_verifier_mechanical.sh` /
  `mini_ork.gates.citation_verifier_mechanical`
- `lib/promotion_gate.sh` / `mini_ork.gates.promotion_gate`

Live shell consumers include `gates/coalition.sh`, `gates/stability.sh`,
`gates/liveness.sh`, `gates/panel-health.sh`, `gates/synthesis-promote.sh`,
oracle auto-wire tests, promotion tests, and the learning/evaluation stack.

Migrate gates as dependency clusters. `promotion_gate` cannot retire safely
before benchmark, utility, version registry, and gate-registry dependencies are
native or explicitly retained external boundaries.

Required behavior coverage includes return codes, fail-open/fail-closed rules,
threshold boundaries, evidence payloads, registry lookup, ordering, and DB
side effects.

### C. Policy, routing, cost, and runtime control

Known ownership pairs include:

- `lib/policy_store.sh` / `mini_ork.stores.policy_store`
- `lib/decision_service.sh` / `mini_ork.steering.decision_service`
- `lib/config_resolve.sh` / `mini_ork.dispatch.config_resolve`
- `lib/lane-helpers.sh` / `mini_ork.dispatch.lane_helpers`
- `lib/pricing_strategy.sh` / `mini_ork.dispatch.pricing_strategy`
- `lib/cost_pause.sh` / `mini_ork.dispatch.cost_pause`
- `lib/deadline_budget.sh` / `mini_ork.dispatch.deadline_budget`
- `lib/throttle-guard.sh` / `mini_ork.dispatch.throttle_guard`
- `lib/scaffold_tier.sh` / `mini_ork.orchestration.scaffold_tier`
- `lib/active_state_index.sh` / `mini_ork.orchestration.active_state_index`

Additional live dependencies include `lib/lane_router.sh`,
`lib/process_reward.sh`, the shared-brain smoke test, executor routing, and
provider dispatch.

Preserve lane selection, capability assertions, per-run config precedence,
cost circuit behavior, retry classification, process-reward inputs, and
operator overrides. Do not replace intentional provider/Git subprocess
boundaries merely to eliminate Bash.

### D. Steering, context roles, and operator intervention

Known ownership pairs include:

- `lib/context_role_packs.sh` / `mini_ork.steering.context_role_packs`
- `lib/operator_steering.sh` / `mini_ork.steering.operator_steering`
- `lib/steering_checkpoint.sh` / `mini_ork.steering.steering_checkpoint`
- `lib/mid_node_injector.sh` / `mini_ork.steering.mid_node_injector`
- `lib/mo_node_events.sh` / `mini_ork.observability.node_events`

Current shell callers include `bin/_worker-launcher.sh`, role-pack smoke tests,
and operator-steering fixtures. Context assembler itself is already native; do
not reopen that completed fork.

Preserve role-pack precedence, ContextNest-down behavior, empty-result
fallbacks, bounded payloads, opt-out controls, checkpoint idempotency, and
stdout/stderr discipline.

### E. Lifecycle, recovery, orchestration, and coordination

Known ownership pairs include:

- `lib/checkpoint.sh` / `mini_ork.stores.checkpoint`
- `lib/finalize.sh` / `mini_ork.recovery.finalize`
- `lib/recursive_orchestration.sh` /
  `mini_ork.orchestration.recursive`
- `lib/epic_graph.sh` / `mini_ork.orchestration.epic_graph`
- `lib/coord_registry.sh` / `mini_ork.registries.coord_registry`
- `lib/coord_gate.sh` / `mini_ork.gates.coord_gate`
- `lib/branch_quarantine.sh` / `mini_ork.vcs.branch_quarantine`
- `lib/repo_integrity_guard.sh` / `mini_ork.vcs.repo_integrity_guard`
- `lib/safety_events.sh` / `mini_ork.stores.safety_events`
- `lib/auto-merge.sh` / `mini_ork.vcs.auto_merge`
- `lib/auto-merge-pr.sh` / `mini_ork.vcs.auto_merge_pr`
- `lib/pr-create.sh` / `mini_ork.vcs.pr_create`

`lib/finalize.sh` sources auto-merge and PR creation, so treat that set as one
dependency graph. Coordination tests source both registry and gate libraries;
migrate them together when their contracts cannot be isolated.

Preserve checkpoint durability, resume semantics, rollback/quarantine status,
branch safety, cascade ordering, pause/budget exits, and public CLI exit codes.

### F. Artifacts, contracts, observability, and persistence

Known ownership pairs include:

- `lib/artifact_contract.sh` / `mini_ork.gates.artifact_contract`
- `lib/harness_wrapper.sh` / `mini_ork.orchestration.harness_wrapper`
- `lib/mo_otel.sh` / `mini_ork.observability.otel`
- `lib/db_open.sh` / `mini_ork.stores.db_open`
- `lib/memory.sh` / `mini_ork.memory.store`
- `lib/pattern_store.sh` / `mini_ork.stores.pattern_store`
- `lib/anchor_corpus.sh` / `mini_ork.stores.anchor_corpus`
- `lib/bug_report.sh` / `mini_ork.observability.bug_report`

Preserve artifact path security, schema-adaptive inserts, hashes and byte
counts, compression/retention behavior, WAL/busy-timeout semantics, trace and
span propagation, contract validation, and missing-table best-effort behavior.

Do not commit databases, transcripts, run directories, or generated evidence.

### G. Learning, evaluation, evolution, and promotion

Known ownership pairs include:

- `lib/benchmark_suite.sh` / `mini_ork.learning.benchmark_suite`
- `lib/utility_function.sh` / `mini_ork.learning.utility_function`
- `lib/version_registry.sh` / `mini_ork.registries.version_registry`
- `lib/group_evolver.sh` / `mini_ork.learning.group_evolver`
- `lib/role_evolver.sh` / `mini_ork.learning.role_evolver`
- `lib/cross_epic_gradient.sh` / `mini_ork.learning.cross_epic_gradient`
- `lib/blame_attributor.sh` / `mini_ork.observability.blame_attributor`
- `lib/rho_aggregator.sh` / `mini_ork.learning.rho_aggregator`
- `lib/topology.sh` / `mini_ork.orchestration.topology`
- `lib/topology_metrics.sh` / `mini_ork.observability.topology_metrics`
- `lib/verifier_rubric.sh` / `mini_ork.gates.verifier_rubric`
- `lib/rubric-prescreen.sh` / `mini_ork.gates.rubric_prescreen`

The benchmark, utility, promotion, and version-registry tests form a coupled
E2E cluster. Preserve candidate status transitions, baseline selection,
utility math, promotion/quarantine outcomes, reward attribution, topology
metrics, and deterministic benchmark summaries.

Reflection and gradient extraction are already closed. This workstream covers
their remaining downstream consumers, not the retired reflection libraries.

### H. Agents, roles, healing, cleanup, and supporting libraries

Known ownership pairs include:

- `lib/agent_registry.sh` / `mini_ork.registries.agent_registry`
- `lib/healer.sh` / `mini_ork.recovery.healer`
- `lib/mo-healer-bridge.sh` / `mini_ork.recovery.healer_bridge`
- `lib/cleaner.sh` / `mini_ork.recovery.cleaner`
- `lib/recovery_planner.sh` or other live recovery surfaces mapped to their
  native owners

Refresh the inventory for exact names and callers. Preserve registry status,
performance aggregates, healer proposal boundaries, cleanup allowlists,
rollback behavior, and external-command safety.

### I. Bash integration, E2E, live, smoke, security, and benchmark fixtures

Fixture conversion is a first-class workstream, not cleanup after runtime
migration. Current live examples include:

- `tests/e2e/test_e2e_benchmark_run.sh`
- `tests/e2e/test_e2e_promotion_gate.sh`
- `tests/e2e/test_e2e_version_registry_rollback.sh`
- `tests/e2e/test_e2e_workflow_candidate_proposal.sh`
- `tests/e2e/test_e2e_reflection_pipeline.sh`
- `tests/integration/test_coord_gate.sh`
- `tests/integration/test_oracle_gates_auto_wire.sh`
- `tests/integration/test_autonomous_epic_pipeline.sh`
- `tests/live/phase_e_live_validation.sh`
- role-pack, ContextNest, and shared-brain smoke scripts
- performance and duration fixtures

Convert a fixture only when its protected behavior is preserved. Delete
fixtures made obsolete by an exact retired owner; retain intentional public CLI
and external-adapter probes.

### J. Dormant, duplicated, dead, and external-adapter decisions

Every `mini_ork/ported/*.py` module must finish with one decision:

- **integrate** into the canonical Python runtime;
- **retain** as an intentional boundary with a named owner;
- **delete** only after all deletion gates pass;
- **defer** with a documented evidence gap and owner.

No-reference scans alone do not authorize deletion. Check product docs, Git
history, dynamic path construction, tests, fixtures, benchmarks, schemas, and
operator contracts.

## Required inventory artifacts

Generate these under a temporary run directory; never commit them:

- `ported-module-inventory.json`
- `ownership-map.json`
- `history-intent.md`
- `proposed-queue.json`
- `pre-change-contract.json`
- `verification.json`
- `requirements-audit-1.md`
- `requirements-audit-2.md`
- `verdict.json`

The inventory must cover every current `mini_ork/ported/*.py` module and every
remaining matching Bash owner, plus unmatched Bash libraries that still sit on
supported runtime paths.

## Provider and agent policy

Allowed agentic lanes:

- Kimi: broad discovery and history synthesis;
- Codex: implementation and focused repair;
- GLM 5.2: independent ownership review and final verdict;
- deterministic scripts: all pass/fail gates.

Forbidden: MiniMax, DeepSeek, Opus, and implicit provider fallback.

Credentials are process-local only and loaded from the operator scripts under
`~/ps/scripts`. Never print, copy, commit, or persist credential values.

The checked-in `self-migrate` recipe may still name obsolete lane aliases. Use
a temporary `MINI_ORK_HOME` with an approved lane map, or run the recipe in
dry-run mode until its lane configuration is proven compliant.

## Per-unit implementation protocol

1. Fetch `origin/main` and create a fresh isolated worktree.
2. Run `bin/mini-ork validate` before editing.
3. Refresh callers, sources, dynamic references, tests, fixtures, benchmarks,
   docs, and Git history for the selected seam.
4. Capture deterministic pre-change behavior while the Bash oracle exists.
5. Implement or adopt one canonical Python owner.
6. Rewire every in-scope runtime caller.
7. Replace Bash-oracle tests with standalone golden/native tests.
8. Run focused unit, integration/E2E/security, and Pyright checks.
9. Run ownership, duplicate, retirement, secret, and OSS-scope scans.
10. Perform requirements audit 1 against the task file and relevant `docs/`.
11. Repair gaps, then perform requirements audit 2 independently.
12. Commit explicit paths only, merge to clean `main`, push, and prove the
    commit is on `origin/main`.
13. Refresh the global inventory before choosing the next unit.

If an audit finds an unresolved requirement, create
`docs/todos/<timestamp>-<unit>.md` with status, last-worked time, remaining
parts, evidence, and suggested implementation. Requeue the unit; do not waive
the requirement.

## Validation matrix

### Every unit

```bash
python3 -m pytest <focused-test-paths> -q -p no:cacheprovider
python3 -m pyright <changed-python-modules>
bin/mini-ork validate
bin/mini-ork garden
git diff --check
git status --short
```

Also run the affected integration/E2E/security/benchmark probe. A pure
deterministic unit does not need a paid provider call.

### Provider or LLM-boundary units

After deterministic stubs pass, run one bounded real probe through the public
path with Codex, Kimi, or GLM as appropriate. Record only redacted evidence.
No MiniMax request is permitted.

### Entry point or library retirement

Run a zero-reference scan before and after deletion:

```bash
rg -n '<retired-name>|source .*<retired-name>|\. .*<retired-name>' \
  bin lib mini_ork gates recipes scripts tests config docs
```

Classify historical documentation separately from executable/runtime/test
edges. Any surviving executable edge fails closure.

### Final whole-program gate

```bash
python3 -m pytest -q -p no:cacheprovider
python3 -m pyright mini_ork
bin/mini-ork validate
bin/mini-ork garden
git diff --check
```

Then regenerate the full ownership inventory and prove:

- every ported module has a terminal decision and owner;
- no useful implementation is orphaned;
- no supported behavior has multiple canonical implementations;
- every retired Bash file has zero executable, test, fixture, and benchmark
  dependency;
- all explicitly retained Bash boundaries have written rationale and owner.

## OSS-readiness gate

Before every commit and again before final completion:

- inspect the complete diff;
- scan for credential values, tokens, private endpoints/IPs, personal paths,
  local provider configs, run IDs with sensitive data, and proprietary assets;
- exclude `.mini-ork/`, state databases, run artifacts, generated inventories,
  transcripts, logs, caches, temporary homes, and unrelated files;
- preserve license headers and public documentation accuracy;
- stage with explicit pathspecs only.

## Merge and push protocol

```bash
git status --short
git diff --check
git add <explicit owned files>
git commit -m '<conventional commit message>'

cd /Volumes/docker-ssd/ps/mini-ork-frc
git fetch origin main
git merge --ff-only <unit-branch>
<rerun focused checks on merged main>
git push origin main
test "$(git rev-parse main)" = "$(git rev-parse origin/main)"
git merge-base --is-ancestor <unit-commit> origin/main
```

If fast-forward is impossible, rebase or recreate the unit from fresh
`origin/main` in isolation. Never force-push, reset shared history, or merge
unrelated user-owned changes.

## Stop conditions

Stop and defer the current unit when:

- current product intent cannot be reconstructed;
- a unique Bash behavior lacks a native owner;
- deterministic validation fails;
- a real-provider probe is required but approved credentials are unavailable;
- the diff contains unrelated or non-OSS-ready files;
- a clean merge to current `origin/main` cannot be achieved safely.

The overall migration does **not** stop merely because one unit is deferred.
Document the blocker, keep the Bash owner, refresh the queue, and continue with
another dependency-safe unit.

## Completion definition

The whole migration is finished only when all of the following are true:

- every current ported module and remaining Bash runtime library has a named
  owner and terminal decision;
- dispatcher/provider/telemetry/tool-grant closure is complete;
- gate and safety libraries are native or intentionally retained;
- policy, routing, cost, and runtime-control libraries are closed;
- steering, role, lifecycle, recovery, coordination, and artifact libraries
  are closed;
- learning/evaluation/promotion libraries and their E2E fixtures are closed;
- Bash integration/E2E/security/benchmark blockers are converted or retained
  with explicit external-boundary rationale;
- final global tests, Pyright, validate, garden, closure scans, requirements
  audits, and OSS-readiness checks pass;
- local `main` equals `origin/main`, and every unit commit is an ancestor of
  `origin/main`.

Queue exhaustion is not proof. The refreshed final inventory and global
closure scan are the independent evidence of completion.

## Ready-to-assign instruction

Assign the next agent this document and instruct it:

> Continue autonomously from current `origin/main`. Refresh the complete
> ownership inventory, select the next dependency-safe ownership seam, use
> mini-ork validation, preserve old behavior before retirement, commit only
> OSS-ready migration files, merge each passing unit into clean `main`, push,
> verify the remote commit, refresh the inventory, and continue until the
> completion definition in this handoff is satisfied or every safe remaining
> unit is explicitly deferred with evidence.

