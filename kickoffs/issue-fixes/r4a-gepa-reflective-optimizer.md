# R4a: native GEPA-style reflective prompt optimizer (standalone, not yet wired)

## Context
Recommendation R4 (`internal-docs/research/impl-analysis/03-gepa-reflective-optimization.md`):
GEPA (reflective prompt evolution) beats GRPO by ~10% using up to 35× fewer rollouts — directly
attacking mini-ork's per-run cost. mini-ork already has a GRPO loop + a PRM (`lib/process_reward.sh`)
+ textual gradients (`lib/gradient_extractor.sh`) that improve recipe prompts from run traces.

This phase adds a native, dependency-free, GEPA-style reflective optimizer as a SEPARATE, opt-in
track alongside GRPO. GRPO (lane routing = which model) is untouched; GEPA optimizes prompt TEXT
(what the model is told). Standalone module + test only; wiring into the reflect→improve stage is
R4b. Reimplement the pattern natively in Python (NOT vendoring the `gepa` pip package / litellm),
mirroring the minimal-agent precedent.

## Deliverables
1. `mini_ork/optimize/gepa.py` — a minimal reflective optimizer:
   - **Adapter protocol** `GepaAdapter` with two methods (the whole integration surface):
     `evaluate(batch, candidate) -> list[float] scores + traces`, and
     `make_reflective_dataset(candidate, eval_batch) -> per-example textual feedback dicts`.
   - **Optimizer loop** `optimize(seed_candidate: dict[str,str], adapter, *, minibatch=8, budget)`:
     Pareto-select a parent candidate → draw a minibatch → evaluate with trace capture → build the
     reflective dataset (failure feedback) → ask a reflection model for a targeted rewrite of ONE
     prompt component → evaluate the mutation on the SAME minibatch → **accept only if the summed
     score improves** (the minibatch-acceptance gate = the 35× rollout saving) → keep a Pareto
     front of candidates. `candidate` is `dict[str,str]` (recipe prompt fields = the textual
     params GEPA evolves).
   - Reflection model call goes through `mini_ork.dispatch.dispatch_model` (reuse the existing
     dispatch; no litellm).
   - Return the best candidate + a trace of accepted mutations.
2. `mini_ork/optimize/__init__.py` exporting `GepaAdapter`, `optimize`.

## Smoke / DoD (must pass)
- `tests/test_gepa_optimizer_py.py` (pytest):
  - A stub `GepaAdapter` over a trivial scorable task (e.g. candidate prompt must contain a target
    token; score = fraction of minibatch examples "solved") + a stub reflection model (monkeypatch
    `dispatch_model`) that proposes the improving edit. Assert: `optimize` returns a candidate with
    a strictly-higher score than the seed; the minibatch-acceptance gate REJECTS a non-improving
    mutation (assert a bad reflection is not accepted); the loop halts at `budget`.
  - Assert rollout economy: full-eval count stays bounded (the acceptance gate prevents evaluating
    every mutation on the full set).
- `python -m pytest -q` overall still green (additive; nothing imports it yet).
- `python -c "import mini_ork.optimize"` works.

## Constraints (scope guard)
- Add ONLY `mini_ork/optimize/gepa.py`, `mini_ork/optimize/__init__.py`, `tests/test_gepa_optimizer_py.py`.
- Do NOT wire into `lib/reflection_pipeline.sh`, `bin/mini-ork-reflect`, or the GRPO loop (that is R4b).
- No new pip dependency (`gepa`, `dspy`, litellm) — native Python + `mini_ork.dispatch` only.
- GRPO path unchanged; this optimizer only runs when explicitly invoked (default system behavior unchanged).
