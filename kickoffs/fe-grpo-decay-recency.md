# Framework Edit: decay GRPO advantage so the router tracks model drift

## Goal

Make `relative_advantage` a **recency-weighted** estimate instead of an
all-time overwrite, so the router can notice when a once-strong lane degrades
(provider model swap, quality regression, price/latency change) and when a
once-weak lane improves. Today the advantage has no notion of time: the most
recent batch silently overwrites history, and stale wins never expire.

This is critique wave 2 (#4 no decay → blind to drift).

## Root cause (verified against bin/mini-ork-execute)

`mo_learning_write_grpo_advantages` upserts the freshly computed advantage with
a full-overwrite `ON CONFLICT`:

```
378   ON CONFLICT(agent_version_id, task_class) DO UPDATE SET
379       role=excluded.role,
380       model=excluded.model,
381       runs_count=excluded.runs_count,
382       success_count=excluded.success_count,
383       avg_cost_usd=excluded.avg_cost_usd,
384       avg_duration_ms=excluded.avg_duration_ms,
385       relative_advantage=excluded.relative_advantage,
386       last_updated=excluded.last_updated
```

`relative_advantage` is replaced by `excluded.relative_advantage` — the value
computed from whatever trace window this run happened to scan. There is no
half-life, no exponential moving average, no time-decay weighting, and no
windowing of the underlying traces by recency. Two failure modes follow:

- **Stale-win blindness.** A lane that earned `+1.0` six months ago keeps that
  `+1.0` until a new batch happens to recompute it. If that lane stops being
  dispatched (because the greedy router favors a rival), its advantage is frozen
  in time and never decays toward neutral.
- **Drift blindness.** When a provider silently swaps the model behind a lane,
  the lane's *new* mediocre traces are averaged flat against its *old* great
  traces (the scan window is not recency-weighted), so the regression is diluted
  and the router keeps trusting the lane.

## Scope Hint

- `bin/mini-ork-execute`
  - GRPO advantage computation `mo_learning_write_grpo_advantages` (~:347-391)

## Expected Edit

Touch exactly one file (`bin/mini-ork-execute`):

Introduce **time-decayed advantage** with two composable mechanisms (implement
at least the EMA blend; the per-trace weighting is recommended):

1. **EMA blend on write.** Instead of overwriting `relative_advantage`, blend
   the newly computed batch advantage with the stored prior:
   `new = alpha * batch + (1 - alpha) * prior`, where `alpha` is env-overridable
   (`MO_LEARNING_DECAY_ALPHA`, recommend `0.30`). A fresh row (no prior) takes
   the batch value directly. This makes a single bad batch nudge, not erase, a
   long-standing advantage, and a string of bad batches steadily pulls it toward
   neutral.

2. **Recency-weight the in-batch scores (recommended).** When aggregating a
   lane's z-scores at `:369`, weight each trace by `exp(-lambda * age_days)`
   using `created_at`, so newer traces dominate the batch advantage. `lambda`
   env-overridable (`MO_LEARNING_HALFLIFE_DAYS`, recommend a 14-day half-life).

## Requirements

- Do not change the PRM heuristic, the reward() fallback, the router gate, or
  any schema/migration. (The `relative_advantage` and `last_updated` columns
  already exist; reuse them.)
- Do not touch `.mini-ork/config/**` or any provider wrapper.
- `MO_LEARNING_DECAY_ALPHA=1.0` must reproduce today's exact overwrite behavior
  (backward compatible escape hatch).
- Pure bash + python3 stdlib; no new dependencies; no numpy.

## Done When

- `${MINI_ORK_RUN_DIR}/framework-edit.diff` contains the single-file patch.
- `${MINI_ORK_RUN_DIR}/verdict.json` contains
  `{ "files_changed": 1, "tests_pass": true, "static_pass": true, "pass": true }`.
- **Decay proof:** seed a lane row with `relative_advantage=+1.0`. Run a batch
  whose computed advantage for that lane is `-1.0` (degraded traces) with
  `MO_LEARNING_DECAY_ALPHA=0.30`. Assert the stored value lands at
  `0.30*(-1.0) + 0.70*(+1.0) = +0.40` (within rounding), **not** `-1.0`. Run the
  same degraded batch three more times and assert the stored value crosses below
  0 (the lane loses its lead gradually). Write the per-iteration values to
  `${MINI_ORK_RUN_DIR}/decay-proof.txt`.
- **Backward-compat proof:** with `MO_LEARNING_DECAY_ALPHA=1.0`, assert the
  stored value equals the raw batch advantage (overwrite semantics preserved).
- `bash scripts/learning-loop-closure-gate.sh` still exits 0.

## Why this kickoff exists

A router that learns must also *un*learn. Right now an advantage is a snapshot
that either gets clobbered or frozen — never aged. When MiniMax, GLM, or Kimi
silently rev their hosted model (which happens without a version bump), the lane
that was best last month can quietly become the worst, and mini-ork keeps
routing to it on month-old evidence. Decay turns `relative_advantage` from a
stale fact into a living estimate. Sources: arXiv:2412.07618, 1405.3316
(non-stationary bandits / discounted value estimates).
