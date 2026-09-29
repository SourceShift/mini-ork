# mini-ork RSI engine — state snapshot (2026-09-29)

Hand-maintained input for the impact reviewers. Point `MO_RSI_STATE_FILE` at a
newer copy to refresh it without editing the recipe. Anchors are
`module:line` in the mini-ork repo; see `docs/reference/FEATURE-INVENTORY.md`.

## Shipped and acting (the loop uses these today)

- **Loop**: classify → plan → execute → verify → reflect → improve → eval →
  promote. Recipes are DAGs of LLM nodes + deterministic verifier nodes.
- **Router**: cost-free contextual bandit (UCB) over lanes
  (`mini_ork/lane_router.py`), plus UCCI isotonic-calibrated error probability
  with cost-constrained threshold escalation to a frontier lane, EntroRouter
  entropy schedule / recovery floor, EquiRouter rank-learning (fc8d05f5).
  `route_margin` supply is thin: margins come almost only from goal-loop/glm.
  NOT built: RouteMoA pre-inference screening, CoBa generate/verify/stop budget.
- **Learning writeback**: GRPO group-relative advantage per
  (agent_version, role, task_class) with shrinkage + EMA + recency halflife
  (`mini_ork/learning/writeback.py`). Anti-Goodhart reward contract: execution
  status primary, LLM reviewer is veto-only.
- **Reflection → textual gradients → apply loop**: reflection pipeline
  extracts patterns (requires distinct trace ids AND distinct run_ids),
  gradient extractor targets node/prompt/edge/verifier/recipe; apply loop
  materializes GEPA-style mutations and scores them against a frozen per-recipe
  probe set (GRASP-style fix-rate minus regression-rate, zero-regression budget)
  and a per-task no-regression gate; auto-apply sweep is taxonomy-ordered with
  outcome-tagged edit memory (a failed edit is never re-proposed).
- **Promotion gate**: no-measurement → rejected; Δutility ≤ 0 → quarantined;
  counts DISTINCT run_ids (poisoning defense). No human approval branch —
  removal is deliberate, never re-add a human gate.
- **Verifier stack**: gate registry (8 types), deterministic verifiers, eval
  node writes reward columns with the LLM judge demoted to veto-only,
  adversarial test hardening + deterministic rule verifiers (358ffd99),
  grounded rejections with evidence trace ids.
- **Memory**: semantic memory with UPDATE/DELETE/ADD reconcile, RetroAgent
  SimUtil-UCB in the emergent-pattern channel, DeltaMem, LIMBO additive half
  (judgment nodes get more memory; trimming deliberately NOT built).
- **Goal-loop**: waves of child runs against a goal predicate; rollback keeps
  the verified edit; worktree-per-iteration self-improve outer loop.
- **Durable DAG resume**: checkpoints, lease/idempotency, tool receipts.
- **Group-evolver / role-evolver**: propose workflow mutations (8 kinds,
  novelty scoring) and lane retire/split — proposal only.

## Shipped as MEASUREMENT only (acting half deliberately not wired)

- G1 `calibration_backtest` cascade blind-spot metric — nothing thresholds on it.
- G3 `mini-ork gate-fuzz` adversarial gate breaker — fuzz failures are not fed
  back as gates.
- G4 memory-lifecycle retire/reactivate — no automatic eviction policy.
- G5 `harness-contrast` harness-vs-model attribution — loop does not promote on it.
- G6 oversight inbox — executor never enqueues (0 rows live).
- G7 `collapse-check` score/anchor divergence — no circuit breaker halts on it.
- `garden` reports installed-but-inert mechanisms from the trace DB.

## In flight (kickoffs written, not merged)

E1 code-arm probe harness (measure a code improvement), E2 probe-set growth
from failures, E3 sequential acceptance certificate (e-value style), E4
edit-scope declaration + rollback targets, E5 harness-effect isolation, E6
point the apply sweep at the harness, E7 route each failure to its source
layer, E8 unfireable kernel (immutable core the loop cannot edit).

## Known weaknesses (measured)

- Verifier is the bottleneck: on SWE-bench the cheap lane generated 3/6
  correct fixes but its gate rejected all 3 → shipped 0. The loop certifies
  non-regression, not correctness; the oracle has a measured hole
  (recall ~84%, n=21).
- Execution-anchored single tests are gameable via coverage gaps; only
  metamorphic/invariance checks close that.
- Compounding curve is FLAT for both weak (directive) and strong
  (prompt-rewrite) levers — bottleneck is corpus/probe headroom, not the
  update rule.
- Most procedural "skills" give ~zero gain (SWE-Skills-Bench: 39/49 zero);
  mini-ork's claim rests on recipes being executable graphs with verifiers.
- Model diffs: 77% of model-emitted unified diffs fail `git apply`; models
  must edit files and let git compute the diff.
- Live coverage gap: many shipped RSI paths are proven by unit tests but have
  not fired in a live `mini-ork run`.
- Lanes: only glm, minimax, deepseek are live; codex dead; Anthropic lanes
  out of scope for the loop's own work.
