# Parity-port registry — retired Bash counterparts

*Historical registry from the 2026-07-26 unused/integration audit
(`docs/audits/2026-07-26-unused-integration-audit.md`), closed 2026-07-29.*

The Python modules below are the canonical runtime owners. Their Bash
counterparts have been retired, and former parity checks are now native unit
tests. The old path is retained here only to aid migration archaeology and
consumer migration; it is not a runnable fallback.

Status legend: **retired** = native owner verified; Bash counterpart deleted.

| Module | Retired Bash counterpart | Status |
|---|---|---|
| `mini_ork/cache.py` | `lib/cache.sh` | retired |
| `mini_ork/trace_store.py` | `lib/trace_store.sh` | retired |
| `mini_ork/memory/store.py` | `lib/memory.sh` | retired |
| `mini_ork/dispatch/pricing_strategy.py` | `lib/llm-dispatch.sh` (pricing) | retired |
| `mini_ork/dispatch/throttle_guard.py` | `lib/throttle_guard.sh` | retired |
| `mini_ork/gates/adaptive_stability.py` | `lib/adaptive_stability.sh` | retired |
| `mini_ork/gates/citation_verifier_mechanical.py` | `lib/citation_verifier.sh` | retired |
| `mini_ork/gates/coalition_gate.py` | `gates/coalition.sh` | retired |
| `mini_ork/gates/common.py` | `lib/gates_common.sh` | retired |
| `mini_ork/gates/cw_por.py` | `lib/cw_por.sh` | retired |
| `mini_ork/gates/honest_ci_gate.py` | `lib/honest_ci_gate.sh` | retired |
| `mini_ork/gates/krippendorff_alpha_gate.py` | `lib/krippendorff_alpha.sh` | retired |
| `mini_ork/gates/mutation_adversary.py` | `lib/mutation_adversary.sh` | retired |
| `mini_ork/gates/refute_or_promote_gate.py` | `lib/refute_or_promote.sh` | retired |
| `mini_ork/gates/scope_overlap.py` | `lib/scope_overlap.sh` | retired |
| `mini_ork/gates/verifier_rubric.py` | `lib/verifier_rubric.sh` | retired |
| `mini_ork/learning/failure_classifier.py` | `lib/failure_classifier.sh` | retired |
| `mini_ork/learning/reflection_refiner.py` | `lib/reflection_refiner.sh` | retired |
| `mini_ork/learning/role_evolver.py` | `lib/role_evolver.sh` | retired |
| `mini_ork/learning/utility_function.py` | `lib/utility_function.sh` | retired |
| `mini_ork/observability/blame_attributor.py` | `lib/blame_attributor.sh` | retired |
| `mini_ork/observability/check_claude_invocations.py` | `bin/mo-check-claude-invocations` | retired |
| `mini_ork/observability/emit_hook.py` | `lib/emit_hook.sh` | retired |
| `mini_ork/observability/langfuse_score_mapper.py` | `lib/langfuse_score_mapper.sh` | retired |
| `mini_ork/observability/otel.py` | `lib/otel.sh` | retired |
| `mini_ork/orchestration/harness_wrapper.py` | `lib/harness_wrapper.sh` | retired |
| `mini_ork/orchestration/spec_split.py` | `lib/spec_split.sh` | retired |
| `mini_ork/policies/engine.py` (+ `policies/`) | (policy engine; no bash twin — staged feature) | retired |
| `mini_ork/recovery/cleaner.py` | `lib/cleaner.sh` | retired |
| `mini_ork/recovery/finalize.py` | `lib/finalize.sh` | retired |
| `mini_ork/recovery/healer.py` | `lib/healer.sh` | retired |
| `mini_ork/recovery/healer_bridge.py` | `lib/healer_bridge.sh` | retired |
| `mini_ork/recovery/trace.py` | `lib/recovery_trace.sh` | retired |
| `mini_ork/registries/agent_registry.py` | `lib/agent_registry.sh` | retired |
| `mini_ork/steering/mid_node_injector.py` | `lib/mid_node_injector.sh` | retired |
| `mini_ork/steering/steer.py` | `bin/mo-steer` | retired |
| `mini_ork/steering/steering_checkpoint.py` | `lib/steering_checkpoint.sh` | retired |
| `mini_ork/stores/anchor_corpus.py` | `lib/anchor_corpus.sh` | retired |
| `mini_ork/stores/db_open.py` | `lib/db_open.sh` | retired |
| `mini_ork/stores/migrate.py` | `db/init.sh` (migration loader) | retired |
| `mini_ork/stores/policy_store.py` | `lib/policy_store.sh` | retired |
| `mini_ork/stores/runs_tracker.py` | `lib/runs_tracker.sh` | retired |
| `mini_ork/stores/safety_events.py` | `lib/safety_events.sh` | retired |
| `mini_ork/stores/tool_receipts.py` | `lib/tool_receipts.sh` | retired |
| `mini_ork/vcs/auto_merge.py` | `lib/auto_merge.sh` | retired |
| `mini_ork/vcs/auto_merge_pr.py` | `lib/auto_merge_pr.sh` | retired |
| `mini_ork/vcs/branch_quarantine.py` | `lib/branch_quarantine.sh` | retired |
| `mini_ork/vcs/pr_create.py` | `lib/pr_create.sh` | retired |
| `mini_ork/vcs/rebase_guard.py` | `lib/rebase_guard.sh` | retired |
| `mini_ork/vcs/worktree_guard.py` | `lib/worktree_guard.sh` | retired |

## Closure evidence

1. Production callers use the native Python modules; `bin/` compatibility
   launchers re-exec the canonical `mini-ork` dispatcher.
2. Former Bash parity tests were converted to standalone native unit tests.
3. A global closure scan confirms no framework shell files remain in `lib/` or
   `gates/`; full pytest, lint, validate, and garden gates verify the result.
