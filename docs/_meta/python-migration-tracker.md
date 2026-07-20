# Bash → Python migration tracker (ADR-001)

Strangler-fig: Bash stays until each Python port is parity-verified against the
live Bash implementation. After retirement, durable pre-retirement receipts and
standalone Python golden contracts replace tests that require deleted code.

## DONE + verified (16 modules)

### Trunk / Tier A — the learning brain (main repo, bash-parity tests)
| module | python | test | notes |
|---|---|---|---|
| cache.sh | `mini_ork/cache.py` | `test_cache_py.py` (7) | **win #2**: dropped `iter` from match → cross-iteration hits; widened stage set. Proven vs bash (bash misses cross-iter). |
| trace_store.sh | `mini_ork/trace_store.py` | `test_trace_store_py.py` (3) | reward_g write path; 9-payload reward_g parity. Carries **win #1** natively. |
| lane_router.sh | `mini_ork/lane_router.py` | `test_lane_router_py.py` (2) | GRPO advantage (shrinkage/EMA/halflife/tiebreak/3-slices) — bit-parity + preferred_lane. |

### Dispatch (Phase 1, earlier this session)
- `mini_ork/dispatch/` is a live backend behind `MO_DISPATCH_BACKEND=python` in
  `lib/llm-dispatch.sh` (sidecar contract preserved; codex+opus verified).

### Leaf tier (isolated clone `mo-migrate`, golden-parity tests, autonomous loop)
process_reward · similarity · utility_function · topology · pricing_strategy ·
config_resolve · rho_aggregator — 7 modules, ~700 LOC. Resumable loop:
`/tmp/migrate_resumable.sh` (run in a persistent terminal to finish the tier).

## REMAINING (trunk)
- **decision_service.sh** (496) — composition; needs deps ported first:
  coalition_gate.sh, process_reward.sh (done in clone → port to main), config_resolve.sh
  (done in clone → port), recursive_policy.
- **Tier B:** finish `llm-dispatch.sh` (tool-summary sidecar, retire bash). The
  context assembler is native and its Bash owner is retired.
- **Tier C top-level forks:** verify, reflect, classify, plan, CLI, execute, and
  the separate scheduler integration fork are closed.

## Test all ported trunk modules
    cd <repo> && python3 -m pytest tests/unit/test_cache_py.py \
      tests/unit/test_trace_store_py.py tests/unit/test_lane_router_py.py -q

## Session 2 additions (Fable, committed on feat/python-migration)
| module | python | test | notes |
|---|---|---|---|
| coalition_gate.sh | mini_ork/ported/coalition_gate.py | test_coalition_gate_py.py (3) | rho taken as input; measure_rho port deferred |
| decision_service.sh | mini_ork/ported/decision_service.py | test_decision_service_py.py (3) | full decide() surface; composes ported lane_router |
| epic_graph.sh | mini_ork/ported/epic_graph.py | test_epic_graph_py.py (4) | dep DAG + cascade |
| mini-ork-scheduler | mini_ork/scheduler.py | test_scheduler_py.py | **win #1 active**: the public Python launcher owns the concurrent epic pool (MO_SCHED_MAX_PARALLEL); duplicate Bash and ported-Python owners retired |
| cost_pause.sh | mini_ork/ported/cost_pause.py | test_cost_pause_py.py (2) | window-crossing pause + sentinel |

Also landed earlier on this branch: win #3 (mo_grade_run_reward: rubric 0-8 ->
graded reward_g, bash+python), lane-fallback hang-proofing (dispatch_with_fallback
+ role-aware chains in executor).

## REMAINING (trunk)
- context_assembler.sh — RETIRED. `mini_ork/context_assembler.py` owns bounded
  packs, failure/prior-run blocks, ContextNest atoms and recent sessions,
  operator-steering rendering, active-state injection, and its fixture CLI.
- reflection_pipeline.sh + gradient_extractor.sh — RETIRED. Native reflection,
  gradients, routing, and standalone contracts own the complete surface.
- top-level CLI and execute — closed on 2026-07-20. `bin/mini-ork` is the
  Python launcher, `mini-ork execute` routes in-process, and the retired
  `bin/mini-ork-execute` implementation is absent.
- llm-dispatch.sh remainder (tool-summary sidecar; then flip MO_DISPATCH_BACKEND default)
- assorted leaves: workflow_lifecycle, operator_steering, steering_checkpoint,
  mid_node_injector, role_evolver, runs-tracker, spec-split, artifact_contract,
  reflection-refiner, cross_epic_gradient
