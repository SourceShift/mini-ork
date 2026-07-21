# Router cost-free learning — follow-up (wire the core into the loop)

The estimator core landed in PR #163: persistent single-sample baseline
(`lane_slice_baseline`), z-score normalization, UCB ordering in
`preferred_lane`, NeuralUCB tie-break, and the migration that adds the bandit
columns + the reserved D4 propensity columns
(`route_source/route_explore/route_score` on `execution_traces`, currently
NULL). This run wires the remaining deliverables so the core is actually fed and
provable. Still **zero** extra model calls on the default path.

## Scope (files explicitly in scope)
- `lib/decision_service.sh` — D1 ε-reroute + D4 propensity writer
- `lib/reflection_pipeline.sh`, `bin/mini-ork-reflect` — D5 per-node credit
- `scripts/router_replay_eval.py` — new (D6)
- `tests/unit/test_lane_router_py.py` — new bandit assertions (retired `test_lane_router.sh`; its cases now live in the native parity gate)
- `docs/architecture/coevolve-ecosystem.md` — Appendix A1 rewrite

Out of scope: `mini_ork/lane_router.py` estimator internals (already shipped),
provider config, weights/training.

## Success command
```
python3 -m pytest tests/unit/test_lane_router_py.py -q && python3 scripts/router_replay_eval.py --db .mini-ork/state.db
```

## D1b — decision_service ε-reroute
In `lib/decision_service.sh`, when an ε-explore draw fires, pick the
**highest-UCB-uncertainty** lane for the slice (lowest `runs_count` among lanes
clearing the floor) instead of uniform-random across `agents.yaml` candidates.
Gate on `MO_ROUTER_UCB_C > 0`; when 0, keep the current uniform-random ε path.
Accept: a unit test shows the ε path selects the least-sampled eligible lane
when the bandit is on, and uniform-random when off.

## D4 — propensity writer
On every routing decision, write `route_source` ('exploit'|'explore'),
`route_explore` (0|1), and `route_score` (the UCB score used) onto the node's
`execution_traces` row. Nullable; only routed (executor-dispatched) nodes
populate them. Accept: a dispatched run leaves non-null `route_source` on routed
nodes; framework-internal traces stay NULL.

## D5 — per-node credit from the single outcome
In the reflect gradient-stamp path, stop stamping run-level `reward_g`
uniformly on every node's trace; weight per-node credit toward decisive nodes
using the existing per-node `process_reward`/verifier signal, falling back to
uniform when that signal is absent. Pure reweighting of existing signal — no new
model calls. Accept: two nodes in one run with different `process_reward` get
different effective credit feeding `recompute_advantages`.

## D6 — offline replay eval
New `scripts/router_replay_eval.py`: replay logged `execution_traces` and score
whether the new estimator (baseline + z-score + UCB) would pick a
higher-reward lane than the legacy greedy rule, on a held-out slice split. No new
inference. Accept: runs against `.mini-ork/state.db`, prints `old_winrate`,
`new_winrate`, `delta`; a fixture-db smoke passes in CI.

## D7-docs — Appendix A1 rewrite
Update `docs/architecture/coevolve-ecosystem.md` Appendix A1 to describe the
actual mechanism: cost-aware contextual bandit with a persistent single-sample
baseline + UCB selection — NOT canonical GRPO, no off-policy correction.

## Global acceptance
- Flag-gated: `MO_ROUTER_*=0` reproduces current routing exactly.
- No extra per-task model calls on the default path.
- New unit tests for D1b, D4, D5; smoke for D6.
- The success command passes.
