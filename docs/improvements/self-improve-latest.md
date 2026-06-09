# Synthesis — Recursive Self-Improvement, iter 18

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1 | `bench_delta_ok` dead in `no-regression.sh` — verifier passes on benchmark regressions | correctness | Wire `bench_delta_ok` into the `pass` computation; gate on `avg(utility_score) >= ${MINI_ORK_BENCH_UTILITY_THRESHOLD:-0.5}` with an INCONCLUSIVE tier when `n < 3` | `recipes/recursive-self-improve/verifiers/no-regression.sh:39` (single hit, write-only), `:72-76` (pass logic ignores it); arXiv 2603.02601, 2604.00222, 2501.12878 | 0.92 |
| 2 | `_self_improve_record_success` indiscriminately supersedes all `deferred` rows | correctness | Filter the UPDATE by `evidence_paths` overlap with `git diff --name-only` (or, fallback, `category` match); cap with a 7-day temporal decay clause as escape valve | `bin/mini-ork-self-improve:175-179`; current state.db shows `id=2` (meta) + `id=3` (perf) would be wiped by any unrelated success | 0.88 |
| 3 | `mini-ork-plan` dispatches planner LLM even when `profile_status=needs_answers` / confidence < threshold | arch | Add a pre-dispatch gate in `bin/mini-ork-plan`; emit `plan_status: needs_answers` artifact and skip `llm_dispatch` when profile is under-specified | `bin/mini-ork-plan:189-211` reads profile metadata, `:218` dispatches unconditionally; runtime cascade in iter-15/16/17 execute.log (3 failed planner calls in 14s); arXiv 2601.15703, 2605.23414, 2604.16753 | 0.85 |
| 4 | Empty-iter halt threshold = 5 burns ≈$0.25 of planner LLM cost on fast-failure cascades | perf | Lower `MINI_ORK_THROTTLE_EMPTY_ITER_THRESHOLD` default 5→3 AND classify `planner-failure` as immediate-halt (orthogonal to 5153739's provider-throttle class) | `bin/mini-ork-self-improve:129`; iter-15/16/17 cascade (3 planner calls before iter-18 intervened); arXiv 2605.08563 (context-contaminated retries) | 0.78 |
| 5 | `docs/improvements/self-improve-latest.md` is a single moving pointer — every publisher run clobbers prior synthesis | arch | Extend `artifact_contract.yaml.outputs` with a second template `docs/improvements/self-improve-iter-${ITER}-${RUN_TS}.md`; teach publisher to expand the minimal allowlist before copying | `recipes/recursive-self-improve/artifact_contract.yaml:14-15` (single output), `bin/mini-ork-execute:644-790` (copy loop has no templating); arXiv 2601.20727, 2603.16208 | 0.74 |

## Top patch — detailed plan

### Patch 1: Wire `bench_delta_ok` into the no-regression pass gate

**Problem statement.** `recipes/recursive-self-improve/verifiers/no-regression.sh:39` initializes `bench_delta_ok=1` and never reassigns or reads it again — the variable is single-assignment write-only. The `pass` calculation at `:72-76` only checks `syntax_failures` and `report_outcome`, so a patch that degrades benchmark utility scores passes the no-regression gate silently. The outer self-improve loop has no forcing function against benchmark regressions, which means convergence cannot be asserted.

**Evidence.**
- `recipes/recursive-self-improve/verifiers/no-regression.sh:39` — sole occurrence of `bench_delta_ok` per `grep -n bench_delta_ok` (correctness lens reproduction step 1).
- `recipes/recursive-self-improve/verifiers/no-regression.sh:42-58` — `bench_summary` query executes but the result only flows to `$EVIDENCE` log at `:60` and to JSON output at `:92`.
- `recipes/recursive-self-improve/verifiers/no-regression.sh:72-76` — `pass=` computation references `syntax_failures` and `report_outcome` only; `bench_delta_ok` is absent.
- Supersedes `learning_record.id=4` (open, correctness, "Utility-delta threshold in no-regression verifier") with stronger dead-variable evidence; new row in iter-18 should set `outcome='superseded-by'` link to this patch.
- arXiv 2603.02601 (AgentAssay, Bhardwaj 2026) — three-valued PASS/FAIL/INCONCLUSIVE verdict pattern, confidence 0.88 (lens-arxiv.md query #1, rank 1).
- arXiv 2501.12878 (μOpTime, Japke 2025) — variance-aware stability gate when sample is too small, confidence 0.67 (lens-arxiv.md query #1, rank 3).

**Proposed change.** Edit `recipes/recursive-self-improve/verifiers/no-regression.sh`:

1. After the existing `bench_summary` SELECT (around `:42-58`), compute `bench_n` (row count) and `bench_avg` (avg utility_score) from `benchmark_results` for the current `run_id`. Reuse the existing sqlite invocation pattern.
2. Read threshold from env: `BENCH_UTILITY_THRESHOLD="${MINI_ORK_BENCH_UTILITY_THRESHOLD:-0.5}"`. Read inconclusive floor: `BENCH_MIN_N="${MINI_ORK_BENCH_MIN_N:-3}"`.
3. Replace the dead `bench_delta_ok=1` line with a three-state assignment:
   - `bench_delta_ok=1` when `bench_n >= BENCH_MIN_N AND bench_avg >= BENCH_UTILITY_THRESHOLD`.
   - `bench_delta_ok=0` when `bench_n >= BENCH_MIN_N AND bench_avg < BENCH_UTILITY_THRESHOLD` (regression).
   - `bench_delta_ok=2` when `bench_n < BENCH_MIN_N` (inconclusive — treat as pass for back-compat, but emit `benchmark_inconclusive: true`).
4. Extend the `pass=` computation at `:72-76` to require `[ "$bench_delta_ok" != "0" ]`. Inconclusive (`=2`) passes; regression (`=0`) fails.
5. Emit a new JSON key `benchmark_regression` (boolean: `bench_delta_ok == 0`) and `benchmark_inconclusive` (boolean: `bench_delta_ok == 2`) alongside the existing `bench_summary` block.

Estimated diff: ~35-50 LoC in a single file. Comfortably under the 200-LoC cap.

**Regression test.** Add `recipes/recursive-self-improve/verifiers/tests/test_no_regression_bench.sh` (new file, ≤80 LoC) that:

1. Creates a tempdir state.db with the `benchmark_results` schema, inserts 5 rows with `utility_score=0.1` (clearly below 0.5).
2. Invokes `no-regression.sh` with env vars pointing at the temp DB.
3. Parses JSON stdout and asserts `pass == false` AND `benchmark_regression == true`. Assertion text: `"benchmark regression must be caught: expected pass=false benchmark_regression=true, got pass=$pass benchmark_regression=$regression"`.
4. Repeats with 2 rows (below the n=3 inconclusive floor) and asserts `pass == true` AND `benchmark_inconclusive == true`. Assertion text: `"low-n benchmark must be inconclusive not failing: expected pass=true benchmark_inconclusive=true"`.
5. Repeats with 5 rows at `utility_score=0.9` and asserts `pass == true` AND both new flags false. Assertion text: `"healthy benchmark must pass cleanly: expected pass=true benchmark_regression=false benchmark_inconclusive=false"`.

Hook the test into whatever runner the recipe uses (the verifier dir already has the convention).

**Verification.** Existing tests that must continue to pass:
- Any existing `no-regression.sh` invocation in prior iter run dirs that did NOT populate `benchmark_results` — these will now report `benchmark_inconclusive=true` AND `pass=true`, preserving the legacy behavior for empty-benchmark runs.
- `bin/mini-ork-self-improve:356-366` and `_read_verifier_inner` consume `pass` only (per correctness lens blast-radius analysis); the new JSON keys are additive and consumers are unaffected.
- The recipe-level success verifier `v6_lint_or_tests` (Go vet + tests, optional pytest) is unaffected — the change is shell-only.

Expected benchmark deltas:
- `mini-ork.bench.no_regression_runtime`: +5-15 ms per verifier invocation (one extra sqlite SELECT). Negligible.
- `mini-ork.bench.self_improve_iter_throughput`: 0 or slight positive — iterations that previously passed on a degraded benchmark will now correctly halt, saving downstream LLM spend, but adding 1 extra rollback per N iterations where N is the prior false-pass rate.

**Rollback criteria.** Discard this patch if any of:
- The default threshold `0.5` rejects more than 30% of patches across 3 consecutive iterations on a stable codebase baseline (signal: noisy single-run benchmarks). Recovery: raise the inconclusive floor to `n=5` OR switch to a per-iter relative delta instead of absolute threshold.
- The new `benchmark_inconclusive` key breaks a downstream consumer the audit missed. Recovery: drop the new keys but keep the `pass` gate change.
- Any test in `bin/mini-ork-self-improve`'s existing suite regresses. Recovery: revert the whole patch and reopen `learning_record.id=4` for a follow-up iteration.

## Lower-ranked patches

### Patch 2: Filter the deferred→superseded UPDATE

**Problem.** `bin/mini-ork-self-improve:175-179` flips every `deferred` row to `superseded` on any successful commit, with no filter on `evidence_paths`, `category`, or commit-touched files. This silently corrupts the dedupe table the next iteration reads.

**Change.** Modify the SQL to `WHERE outcome='deferred' AND (category=:fixed_category OR EXISTS(SELECT 1 FROM json_each(evidence_paths) WHERE value IN (<git diff --name-only>)))`. Implement the path-overlap check as a Python snippet inside the bash function (per correctness lens open question #2). Add a 7-day `updated_at` decay fallback so unmatched rows age out instead of accumulating forever.

**Test.** Synthetic DB with 2 deferred rows (one with overlapping `evidence_paths`, one without); assert only the overlapping row is superseded after the success-commit call.

**Evidence.** arXiv 2512.10696 (ReMe, Cao 2025, conf 0.82) — utility-based memory refinement vs append-only; arXiv 2601.11974 (MARS, Hou 2026, conf 0.69) — procedural reflection tied to commit evidence.

### Patch 3: Pre-dispatch profile gate in `mini-ork-plan`

**Problem.** `bin/mini-ork-plan:189-211` reads `profile_status` and `confidence` into plan metadata but dispatches the LLM unconditionally at `:218`. iter-15/16/17 execute.log shows 3 identical cascade failures in 14s on `profile_status=needs_answers profile_confidence=0.55`.

**Change.** Before the `PLAN_JSON_RAW=$(llm_dispatch ...)` call, check `profile_status != ready` OR `confidence < ${MINI_ORK_PLAN_CONFIDENCE_FLOOR:-0.7}`. If gated, emit a deterministic plan artifact with `plan_status: needs_answers`, `blocked_by: run_profile`, and `human_questions`, then exit 0 (preserving artifact-write semantics; outer runner already handles the blocked state through throttle logic). Skip the LLM dispatch entirely.

**Test.** Drive `bin/mini-ork-plan` with a synthetic `run_profile.json` containing `profile_status=needs_answers`; assert no `llm_dispatch` invocation AND that `plan.json` contains `plan_status: needs_answers`.

**Evidence.** arXiv 2601.15703 (AUQ, Zhang 2026, conf 0.84) — uncertainty as active control signal; arXiv 2605.23414 (EPC-AW, Wang 2026, conf 0.79) — refuse when inputs under-specified.

### Patch 4: Tighten empty-iter halt threshold + planner-failure immediate halt

**Problem.** `bin/mini-ork-self-improve:129` defaults `EMPTY_ITER_HALT=5`. Three failed planner iters cost ≈$0.15 in LLM spend before the halt fires; iter-18 only intervened because it was a fresh attempt.

**Change.** Lower default to 3. Add a separate fast-path: if the last N failures are all classified as `planner-failure` (distinct from provider-throttle landed in 5153739), halt immediately regardless of `EMPTY_ITER_HALT`. Wire through the existing `lib/throttle-guard.sh` classification path.

**Test.** Simulate 3 consecutive `planner-failure` outcomes; assert the loop halts before the 4th iter starts AND a `learning_record` row is written with `category=meta outcome=halted`.

**Evidence.** arXiv 2605.08563 (CCRM, Yang 2026, conf 0.83) — context-contaminated retries make subsequent attempts strictly worse than clean ones.

### Patch 5: Immutable archive sibling for synthesis artifact

**Problem.** `recipes/recursive-self-improve/artifact_contract.yaml:14-15` declares one output. Every publisher run overwrites `docs/improvements/self-improve-latest.md`. The audit trail collapses at the publisher boundary; future scanners reading only the latest pointer lose path-stable iteration comparison.

**Change.** Add a second `outputs[]` entry with template `docs/improvements/self-improve-iter-${ITER}-${RUN_TS}.md`. Teach `bin/mini-ork-execute:644-790` publisher copy loop to expand a minimal allowlist (`ITER`, `RUN_TS`, `RUN_ID`) before copying. Behind feature flag `MO_ARTIFACT_OUTPUT_TEMPLATES=1` until the contract stabilizes.

**Test.** Run publisher with two distinct `ITER` values; assert both archive paths exist post-run AND `self-improve-latest.md` content matches the most recent.

**Evidence.** arXiv 2601.20727 (Audit Trails, Ojewale 2026, conf 0.86) — chronological tamper-evident ledger linked to governance; arXiv 2603.16208 (SoK Traceability, Chen 2026, conf 0.72) — preserve both consumer pointer and role-specific artifact path.

## Convergence assessment

**Not converging yet.** As long as Bottleneck #1 holds — the no-regression verifier silently passes on benchmark regressions — the outer loop has no forcing function against utility decay. The convergence assertion in `lens-bottleneck.md:64` makes the same point: convergence cannot be claimed until `bench_delta_ok` is wired into the pass gate. The outer loop should NOT terminate after this iteration; instead, the next iteration should pick up Patch 2 (memory-laundering) since it has the largest blast radius on dedupe integrity, and Patches 3-5 should queue via `learning_record` for iters 20-22.

Additionally: open `learning_record` rows id=2 (auto-promote hook) and id=3 (duration_ms capture) remain unaddressed from iter-4 and are now nearly six iterations stale. They should be considered higher-priority than Patches 4-5 once the correctness gates close.

## Provenance footer

- Lenses consumed: minimax (perf, via lens-bottleneck #4/#6), kimi (correctness, lens-correctness.md), codex (arch, lens-arch.md; arxiv, lens-arxiv.md)
- Synthesizer family: opus (Anthropic; only synthesizer node permitted on Anthropic per provider policy)
- arXiv papers cited: 11 (2603.02601, 2604.00222, 2501.12878, 2512.10696, 2601.11974, 2601.15703, 2605.23414, 2604.16753, 2605.08563, 2601.20727, 2603.16208) — all sourced from `lens-arxiv.md`
- Cross-iteration learnings applied: 4 rows from `learning_record` (id=1 resolved/excluded; id=2, id=3 open/deferred and respected; id=4 superseded-with-evidence by Patch 1)
- Excluded as already-merged: 4 commits (2bc9a88, bba5b01, 5153739, b1fc54b)
