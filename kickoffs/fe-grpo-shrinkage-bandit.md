# Framework Edit: shrink tiny-sample advantages and give the router an exploration floor

## Goal

Stop the router from crowning a lane on the strength of one or two noisy runs,
and stop it from locking onto that lane forever. Two coupled changes:

1. **Shrink** the GRPO advantage toward 0 when the group is small, so a single
   lucky run cannot produce a confident `relative_advantage`.
2. **Replace the greedy argmax** in the router with a bandit-with-floor: keep
   exploiting the current best lane most of the time, but reserve a small
   probability mass to explore other eligible lanes so a lane that was unlucky
   early can recover.

This is critique waves 1 (#1 tiny-sample noise + #3 greedy starvation), which
the critique flags as the highest-priority fix because they compose: shrinkage
makes the bandit's value estimates trustworthy, and the bandit makes shrinkage
safe (an over-shrunk lane still gets explored).

## Root cause (verified against bin/mini-ork-execute)

**#1 — raw z-score, no shrinkage.** In `mo_learning_write_grpo_advantages`:

```
355   variance = sum((score - mean) ** 2 for score in scores) / len(scores)
356   std = math.sqrt(variance)
366   bucket["adv"].append(0.0 if std == 0 else (score - mean) / std)
369   rel_adv = sum(bucket["adv"]) / len(bucket["adv"])
```

The advantage is a raw point estimate. A group of size 2 with scores
`{0.9, 0.1}` yields `±1.0` advantage — maximal confidence from one comparison.
There is no shrinkage factor, no confidence interval, no `n`-aware discount. The
value is written straight to `agent_performance_memory.relative_advantage`
(`:370-391`) and the router treats `+1.0`-from-2-runs identically to
`+1.0`-from-200-runs.

**#3 — greedy one-shot router, no exploration.** In `_mo_learning_governed_lane`:

```
208   SELECT agent_version_id, relative_advantage
209     FROM agent_performance_memory
212    WHERE task_class = ?
213      AND runs_count >= ?
214      AND relative_advantage > 0
216    ORDER BY relative_advantage DESC, runs_count DESC
217    LIMIT 1
```

`min_samples` defaults to 3 (`:172`) but clamps to a floor of 1 (any
`MO_LEARNING_MIN_SAMPLES` ≥ 1). The query is a pure argmax: the single highest
`relative_advantage` lane wins every dispatch. There is no ε-greedy floor, no
Thompson/UCB sampling, no decayed re-exploration. A lane that posts a high
advantage from a tiny sample is selected indefinitely; lanes with `runs_count <
min_samples` or a momentarily-negative advantage are never revisited.

## Scope Hint

- `bin/mini-ork-execute`
  - GRPO advantage computation `mo_learning_write_grpo_advantages` (~:351-391)
  - router gate `_mo_learning_governed_lane` (~:170-225)

## Expected Edit

Touch exactly one file (`bin/mini-ork-execute`):

1. **Add `n`-aware shrinkage** to the advantage write. Replace the raw mean of
   z-scores at `:369` with a shrunk estimate, e.g.
   `rel_adv_shrunk = rel_adv * n / (n + k)` where `n = runs_count` and `k` is a
   small constant (recommend `k = 5`, env-overridable
   `MO_LEARNING_SHRINKAGE_K`). At `n=1, k=5` the advantage is discounted to 1/6
   of its raw value; by `n=20` it is ~80% of raw. Store the shrunk value in
   `relative_advantage`. Keep the raw value out of the router's reach.

2. **Add an exploration floor** to the router. Before the greedy argmax, with
   probability `epsilon` (recommend `0.10`, env `MO_LEARNING_EPSILON`) pick a
   *random eligible* lane (any lane with `runs_count >= 1` for this
   `(task_class, node_type)`, including the current static lane) instead of the
   argmax. The exploit branch keeps the existing `ORDER BY relative_advantage
   DESC LIMIT 1`. Seed the RNG from `MO_LEARNING_SEED` when set so the proof
   harness is deterministic.

## Requirements

- Do not change the PRM heuristic, the reward() fallback, or any schema/migration.
- Do not touch `.mini-ork/config/**` or any provider wrapper.
- Both knobs must be env-overridable and default to safe values
  (`MO_LEARNING_SHRINKAGE_K=5`, `MO_LEARNING_EPSILON=0.10`). With
  `MO_LEARNING_EPSILON=0` the router must reproduce today's exact greedy
  behavior (backward compatible).
- Pure bash + python3 stdlib; no new dependencies; no numpy.

## Done When

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the single-file patch.
- `${MINI_ORK_RUN_DIR}/verdict.json` contains
  `{ "files_changed": 1, "tests_pass": true, "static_pass": true, "pass": true }`.
- **Shrinkage proof:** seed a DB with one `(reviewer, chapter_validation)`
  group holding two lenses scored PRM 0.9 / 0.1 at `runs_count=1`. Run
  `mo_learning_write_grpo_advantages`. Assert the winning lane's stored
  `relative_advantage` is **strictly less than** the raw z-score (`< 1.0`, ~0.17
  at `k=5`). Repeat with `runs_count=30` and assert the stored value is
  **closer to** the raw z-score than the `n=1` case. Write both to
  `${MINI_ORK_RUN_DIR}/shrinkage-proof.txt`.
- **Exploration proof:** with a seeded DB where lane A has `relative_advantage=
  +1.0` and lane B has `+0.2`, run the router 1000 times with
  `MO_LEARNING_EPSILON=0.10 MO_LEARNING_SEED=1`. Assert lane B (and the static
  fallback) is selected on **roughly 10%** of dispatches (between 5% and 15%),
  and lane A on the rest. With `MO_LEARNING_EPSILON=0` assert lane A is selected
  100% of the time. Write counts to `${MINI_ORK_RUN_DIR}/exploration-proof.txt`.
- `bash scripts/learning-loop-closure-gate.sh` still exits 0.

## Why this kickoff exists

The live learning loop can now physically flip a lane (FE-1 closed the write
half), but it flips on noise: a z-score from `n=1` is indistinguishable from a
z-score from `n=200`, and once a lane wins the greedy router never lets a rival
recover. Shrinkage makes small-sample advantages humble; the exploration floor
keeps the loop able to correct itself. Together they convert "first lane to get
lucky wins forever" into "lanes earn and keep their lead." Sources:
arXiv:2601.08521, 2508.14094 (small-sample advantage noise), 2506.02933,
2511.05620 (exploration floors / bandit routing).
