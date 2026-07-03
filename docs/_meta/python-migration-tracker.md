# Bash → Python migration tracker (ADR-001)

Strangler-fig: bash stays until each Python port is parity-verified against the
LIVE bash. Every ported module has a test that invokes the real bash function.

## DONE + verified (10 modules)

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
- **Tier B:** finish `llm-dispatch.sh` (tool-summary sidecar, retire bash), `context_assembler.sh` (713, context-engine).
- **Tier C:** `bin/mini-ork-execute` (2942), `bin/mini-ork-plan` (1077),
  `bin/mini-ork-reflect` (247), `bin/mini-ork-scheduler` (300 — concurrent-scheduler win).

## Test all ported trunk modules
    cd <repo> && python3 -m pytest tests/unit/test_cache_py.py \
      tests/unit/test_trace_store_py.py tests/unit/test_lane_router_py.py -q
