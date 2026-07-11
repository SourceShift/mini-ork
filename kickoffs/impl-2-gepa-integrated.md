# IMPL-2 — Make GEPA produce real, accepted prompt improvements ("GEPA integrated")

## Goal (one deliverable)

Turn GEPA from an inert scaffold into an optimizer that can actually accept a prompt mutation.
Two coupled fixes the panel proved must land together (G3 short-circuits before G2 is even reached).
This does NOT apply the accepted suggestion to production prompts — that is IMPL-3.

## Background (validated by the 4-lens panel)

- **G3** the mutation step dispatches `model="stub"`, an unknown lane that returns `_NoProposal`, so
  no rewrite is ever proposed. The `stub` default exists in **two** places — both must change:
  `mini_ork/optimize/gepa.py:148` and `mini_ork/optimize/miniork_adapter.py:311` (`run_suggestion`
  calls `optimize()` without a model).
- **G2 (fatal):** the offline adapter's `evaluate()` scores a candidate by matching
  `candidate['prompt_version_hash']` to cached `reward_value` rows. Seed and mutated candidates have
  no matching hash → both score `_default_score` → parent ≡ child on every minibatch → the strict
  gate `sum(new) <= sum(parent)` (`gepa.py:204`) always rejects. **The optimizer can never accept a
  new prompt because it cannot score a prompt that has never run.**

## Files in scope

- `/Volumes/docker-ssd/ps/mini-ork/mini_ork/optimize/gepa.py`
- `/Volumes/docker-ssd/ps/mini-ork/mini_ork/optimize/miniork_adapter.py`
- `/Volumes/docker-ssd/ps/mini-ork/bin/mini-ork-reflect` (the `MO_OPTIMIZER` enable path)

## Acceptance criteria

1. **Real mutation model (G3):** the mutation step uses a real lane via a new `MO_OPTIMIZER_MODEL`
   env (default to a reliable code lane, e.g. `minimax`), threaded through `run_suggestion` →
   `optimize(model=...)` → `reflect_on_component`. No `stub` default reachable in the reflect path.
2. **Online/held-out evaluator (G2) — the core fix:** add an evaluator that scores a *mutated prompt's
   text* rather than only looking up a historical hash. Minimum viable: run the candidate prompt on a
   small held-out set of that task_class's recent inputs and score with the existing rubric/verifier,
   OR an LLM-judge that compares parent vs mutated output on the reflective examples. The acceptance
   gate must be able to strictly prefer a genuinely better prompt. Keep the offline hash lookup as a
   fast path when a hash match exists.
3. **Provable acceptance:** a test with a deliberately-bad seed prompt and an obviously-better mutation
   shows `full_eval_count > 0` and the returned best candidate ≠ seed. Today this is impossible.
4. **Honest validity (new issue from panel):** `validity:"valid"` must distinguish "ran" from
   "improved" — a run with zero accepted mutations reports `no_improvement`, not `valid`
   (`miniork_adapter.py` run_suggestion). And `insufficient_evidence` suggestions must not be silently
   dropped in `bin/mini-ork-reflect:258-260`.
5. Budget/rollout bound preserved (`full_eval_count <= budget`); suggest-only (no auto-apply here).

## Out of scope

Applying the accepted suggestion to production prompt files (IMPL-3), enabling GEPA by default
(final switch after IMPL-3), gradient pipeline (IMPL-1).

## Verification

- Unit test: bad-seed → better-mutation is accepted (full_eval_count>0, best≠seed).
- Unit test: no real improvement → `validity` is `no_improvement`, not `valid`.
- Smoke: `MO_OPTIMIZER=gepa MO_OPTIMIZER_MODEL=minimax` reflect over a task_class emits a real,
  non-stub suggestion whose candidate differs from the on-disk prompt.
