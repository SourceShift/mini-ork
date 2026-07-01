# R4b: wire the GEPA optimizer into the reflect→improve loop (opt-in, suggest-only)

## Context
R4a landed the engine `mini_ork/optimize/gepa.py` (`GepaAdapter` protocol + `optimize()` with the
minibatch-acceptance gate). It's standalone with a stub-adapter test. R4b makes it real: a
concrete adapter over mini-ork's own trace + reward data, and an OPT-IN hook in the reflect stage
that uses GEPA to propose improved recipe prompts. GRPO (lane routing) is untouched; GEPA evolves
prompt TEXT. This is the compounding-flywheel step — but it touches the learning loop, so it must
be conservative: **opt-in, suggest-only (never auto-applies a prompt), default behavior unchanged.**

Read for grounding: `internal-docs/research/impl-analysis/03-gepa-reflective-optimization.md`,
`db/migrations/0010_benchmarks.sql` (`execution_traces` schema: trace_id, task_class,
prompt_version_hash, verifier_output, reviewer_verdict, cost_usd, status, …), `lib/process_reward.sh`
(the per-trace reward), `lib/gradient_extractor.sh` (textual gradients), `bin/mini-ork-reflect` +
`lib/reflection_pipeline.sh` (where reflect runs).

## Deliverables
1. `mini_ork/optimize/miniork_adapter.py` — a real `GepaAdapter`:
   - `full_batch` = a small set of recent `execution_traces` rows for the target recipe/task_class
     (the "examples").
   - `evaluate(batch, candidate)` → per-example score from mini-ork's reward. Use the HISTORICAL
     trace store as the eval cache where possible (a candidate whose `prompt_version_hash` matches
     existing traces scores from those rows — most GEPA "rollouts" become SQL reads per the
     research). For a novel candidate with no matching traces, return the parent's cached score as
     a conservative proxy (do NOT dispatch live LLM runs in this phase — keep R4b cheap + offline).
   - `make_reflective_dataset(candidate, eval_batch)` → textual failure feedback from each trace's
     `verifier_output`/reviewer notes (reuse the gradient-extraction shape).
2. Opt-in reflect hook: in `bin/mini-ork-reflect` (or `lib/reflection_pipeline.sh`), when
   `MO_OPTIMIZER=gepa` is set, after the normal reflect pipeline, build the adapter for the run's
   recipe, run `optimize(seed=current recipe prompt fields, adapter)`, and **record the best
   candidate as a SUGGESTION** (a promotion-suggestion row / artifact — same surface as existing
   `suggest_promotions`), never writing recipe files. Default (`MO_OPTIMIZER` unset) = current
   reflect path, byte-identical.
3. `mini_ork/optimize/__init__.py` export the adapter.

## Smoke / DoD (must pass)
- `tests/test_gepa_wiring_py.py` (pytest): seed a temp sqlite `execution_traces` fixture with a
  few rows (some low-reward failures), build `MiniOrkGepaAdapter`, run `optimize`, and assert:
  it returns a candidate (improved or equal), the adapter scored from the trace rows (no live
  dispatch — monkeypatch `dispatch_model` for the reflection step only), and a suggestion record
  is produced. Assert the acceptance gate + budget bound hold end-to-end.
- Default-path test: with `MO_OPTIMIZER` unset, the reflect hook is a no-op (assert the optimizer
  is not invoked — e.g. a sentinel/log).
- `python -m pytest -q` green; existing bash reflect tests unaffected.
- `bash -n bin/mini-ork-reflect` (+ any changed lib) clean.

## Constraints (scope guard)
- Opt-in via `MO_OPTIMIZER=gepa`; default unchanged; **suggest-only, never auto-apply a prompt**.
- No live LLM eval in the adapter (offline/trace-cached scoring; reflection step may call
  `dispatch_model`). No new pip dep.
- Touch: `mini_ork/optimize/miniork_adapter.py`, `mini_ork/optimize/__init__.py`,
  `bin/mini-ork-reflect` (or `lib/reflection_pipeline.sh`), `tests/test_gepa_wiring_py.py`. Do NOT
  change GRPO, `mini_ork/optimize/gepa.py` (R4a is frozen), or recipe files.
