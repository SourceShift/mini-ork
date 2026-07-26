# Parity-port registry — Python modules whose production path is still bash

*From the 2026-07-26 unused/integration audit (docs/audits/2026-07-26-unused-integration-audit.md).*

The strangler-fig migration works like this: a bash `lib/*.sh` (or `bin/`,
`gates/*.sh`) implementation is **live**, its Python port exists for the
cutover, and a `tests/unit/test_*_py.py` parity gate pins the two together.
The modules below are **not wired into production** — importing them does
nothing unless a cutover switches the production path. Treat them as
staged-for-cutover, not dead code; do not delete them (their parity tests
are the migration's safety net), and do not import them from production
code without doing the cutover.

Status legend: **staged** = parity tests green, bash still live.

| Module | Bash twin (live) | Status |
|---|---|---|
| `mini_ork/cache.py` | `lib/cache.sh` | staged |
| `mini_ork/trace_store.py` | `lib/trace_store.sh` | staged |
| `mini_ork/memory/store.py` | `lib/memory.sh` | staged |
| `mini_ork/dispatch/pricing_strategy.py` | `lib/llm-dispatch.sh` (pricing) | staged |
| `mini_ork/dispatch/throttle_guard.py` | `lib/throttle_guard.sh` | staged |
| `mini_ork/gates/adaptive_stability.py` | `lib/adaptive_stability.sh` | staged |
| `mini_ork/gates/citation_verifier_mechanical.py` | `lib/citation_verifier.sh` | staged |
| `mini_ork/gates/coalition_gate.py` | `gates/coalition.sh` | staged |
| `mini_ork/gates/common.py` | `lib/gates_common.sh` | staged |
| `mini_ork/gates/cw_por.py` | `lib/cw_por.sh` | staged |
| `mini_ork/gates/honest_ci_gate.py` | `lib/honest_ci_gate.sh` | staged |
| `mini_ork/gates/krippendorff_alpha_gate.py` | `lib/krippendorff_alpha.sh` | staged |
| `mini_ork/gates/mutation_adversary.py` | `lib/mutation_adversary.sh` | staged |
| `mini_ork/gates/refute_or_promote_gate.py` | `lib/refute_or_promote.sh` | staged |
| `mini_ork/gates/scope_overlap.py` | `lib/scope_overlap.sh` | staged |
| `mini_ork/gates/verifier_rubric.py` | `lib/verifier_rubric.sh` | staged |
| `mini_ork/learning/failure_classifier.py` | `lib/failure_classifier.sh` | staged |
| `mini_ork/learning/reflection_refiner.py` | `lib/reflection_refiner.sh` | staged |
| `mini_ork/learning/role_evolver.py` | `lib/role_evolver.sh` | staged |
| `mini_ork/learning/utility_function.py` | `lib/utility_function.sh` | staged |
| `mini_ork/observability/blame_attributor.py` | `lib/blame_attributor.sh` | staged |
| `mini_ork/observability/check_claude_invocations.py` | `bin/mo-check-claude-invocations` | staged |
| `mini_ork/observability/emit_hook.py` | `lib/emit_hook.sh` | staged |
| `mini_ork/observability/langfuse_score_mapper.py` | `lib/langfuse_score_mapper.sh` | staged |
| `mini_ork/observability/otel.py` | `lib/otel.sh` | staged |
| `mini_ork/orchestration/harness_wrapper.py` | `lib/harness_wrapper.sh` | staged |
| `mini_ork/orchestration/spec_split.py` | `lib/spec_split.sh` | staged |
| `mini_ork/policies/engine.py` (+ `policies/`) | (policy engine; no bash twin — staged feature) | staged |
| `mini_ork/recovery/cleaner.py` | `lib/cleaner.sh` | staged |
| `mini_ork/recovery/finalize.py` | `lib/finalize.sh` | staged |
| `mini_ork/recovery/healer.py` | `lib/healer.sh` | staged |
| `mini_ork/recovery/healer_bridge.py` | `lib/healer_bridge.sh` | staged |
| `mini_ork/recovery/trace.py` | `lib/recovery_trace.sh` | staged |
| `mini_ork/registries/agent_registry.py` | `lib/agent_registry.sh` | staged |
| `mini_ork/steering/mid_node_injector.py` | `lib/mid_node_injector.sh` | staged |
| `mini_ork/steering/steer.py` | `bin/mo-steer` | staged |
| `mini_ork/steering/steering_checkpoint.py` | `lib/steering_checkpoint.sh` | staged |
| `mini_ork/stores/anchor_corpus.py` | `lib/anchor_corpus.sh` | staged |
| `mini_ork/stores/db_open.py` | `lib/db_open.sh` | staged |
| `mini_ork/stores/migrate.py` | `db/init.sh` (migration loader) | staged |
| `mini_ork/stores/policy_store.py` | `lib/policy_store.sh` | staged |
| `mini_ork/stores/runs_tracker.py` | `lib/runs_tracker.sh` | staged |
| `mini_ork/stores/safety_events.py` | `lib/safety_events.sh` | staged |
| `mini_ork/stores/tool_receipts.py` | `lib/tool_receipts.sh` | staged |
| `mini_ork/vcs/auto_merge.py` | `lib/auto_merge.sh` | staged |
| `mini_ork/vcs/auto_merge_pr.py` | `lib/auto_merge_pr.sh` | staged |
| `mini_ork/vcs/branch_quarantine.py` | `lib/branch_quarantine.sh` | staged |
| `mini_ork/vcs/pr_create.py` | `lib/pr_create.sh` | staged |
| `mini_ork/vcs/rebase_guard.py` | `lib/rebase_guard.sh` | staged |
| `mini_ork/vcs/worktree_guard.py` | `lib/worktree_guard.sh` | staged |

## Cutover procedure (when a module is promoted to production)

1. Switch the production caller (bin wrapper via `lib/runtime-select.sh`, or
   the Python importer) from the bash twin to the module.
2. Keep the parity test until the bash twin is retired; then convert it to a
   plain unit test and remove the twin.
3. Move the row from this registry to `docs/migration/`'s landed list and
   delete the row here.
