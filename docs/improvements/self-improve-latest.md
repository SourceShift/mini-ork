# Synthesis — Recursive Self-Improvement, iter 1

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1 | JSON verifier results not authoritative in generic executor | correctness | Add `_run_verifier_ref` helper in `bin/mini-ork-execute` that captures verifier stdout, parses JSON, and honors `.pass` field; fall back to exit-code semantics for legacy verifiers | `lens-bottleneck.md:7` (Row 1); `lens-correctness.md:7,17-31,143-173`; `bin/mini-ork-execute:579`; `recipes/recursive-self-improve/verifiers/bottlenecks-found.sh:9`, `self-tests-pass.sh:10`, `no-regression.sh:11`; `bin/mini-ork-self-improve:229-260` workaround; `learning_record.id=1` outcome=deferred; arXiv 2605.17998 (verify-gated completion) | 0.86 |
| 2 | Bottleneck prompt names non-existent `traces` table | correctness | Edit `recipes/recursive-self-improve/prompts/bottleneck-scan.md:13-15` to reference `execution_traces` instead of `traces` | `lens-bottleneck.md:8` (Row 2); `lens-correctness.md:8,32-38,176-192`; `db/migrations/0010_benchmarks.sql:12`; `lib/trace_store.sh:2`; `lib/context_assembler.sh:87-98` | 0.95 |
| 3 | `duration_ms` always 0 in `execution_traces` | correctness/perf | Pass `started_at`/`ended_at` from `task_runs` into `_trace_write_node_rich`; compute `duration_ms` in the python3 payload at `bin/mini-ork-execute:294-308` | `lens-bottleneck.md:9` (Row 3); `lens-perf.md:30-79,166-211`; `lens-correctness.md:10,52-67,212-227`; `_d021_set_status` at `bin/mini-ork-execute:321-339`; `lib/trace_store.sh:76-78`; arXiv 2602.10133 (AgentTrace), 2601.06112 (ReliabilityBench) | 0.85 |
| 4 | Wrapper-pollution check covers only `synthesis.md` | correctness | In `recipes/recursive-self-improve/verifiers/bottlenecks-found.sh:48-50`, expand the CLI-envelope grep loop to all required durable lens artifacts | `lens-bottleneck.md:13` (Row 7); `lens-correctness.md:9,40-50,194-210`; `lens-arch.md:62-69`; prior polluted `self-improve-iter-1-20260609054721/lens-arch.md:10-55`; arXiv 2604.27586 (trace-level contamination) | 0.84 |
| 5 | Cost-circuit forks python3 + queries SQLite on every dispatch | perf | Mirror the `_MO_LANE_<UPPER>` env-cache pattern at `lib/llm-dispatch.sh:373-414` for the cost-circuit check at `lib/llm-dispatch.sh:348-368`, TTL-bound at 30s, key on `MINI_ORK_DB` | `lens-bottleneck.md:11` (Row 5); `lens-perf.md:81-122,127-164`; prior `self-improve-iter-1-20260609054721/lens-perf.md:35-49`; arXiv 2601.06007 (prompt caching), 2512.23049 (prompt choreography) | 0.72 |

## Top patch — detailed plan

### Patch 1: JSON-aware verifier adapter in `bin/mini-ork-execute`

**Problem statement.** The generic executor at `bin/mini-ork-execute:579` gates `verifier_ref` only on shell exit code, but every recursive-self-improve verifier emits JSON to stdout with `exit 0` regardless of `.pass`. The outer runner at `bin/mini-ork-self-improve:229-260` works around this by manually parsing `verifier-result-*.json` with `jq '.pass'`. Any future recipe adding a JSON verifier silently false-passes.

**Evidence.**
- Internal: `bin/mini-ork-execute:579` (the exit-only gate); `recipes/recursive-self-improve/verifiers/bottlenecks-found.sh:9`, `self-tests-pass.sh:10`, `no-regression.sh:11` (all exit 0 by contract); `bin/mini-ork-self-improve:229-260` (workaround that must be kept until the adapter lands).
- Cross-iter: `learning_record.id=1`, category `arch`, title "Verifier verdict JSON adapter (_run_verifier_ref)", outcome `deferred`, severity `medium`, confidence `0.75`, evidence paths `["bin/mini-ork-execute","tests/unit/test_verifier_ref_json.sh"]`. Commit `ec748c0` preserved this deferred patch but did not land it.
- arXiv: **2605.17998** (Nguyen 2026, "Verify-Gated Completion as Admission Control") — argues that completion is an admission-control decision owned by a verifier object whose pass/fail verdict, evidence path, and missing-check list are parsed by the runtime before the workflow advances. Direct support for making `verifier_ref` JSON authoritative. Confidence 0.86.

**Proposed change.**

1. In `bin/mini-ork-execute`, add a helper `_run_verifier_ref <script> <evidence_path>` near the existing dispatch site (around line 575). Pseudocode:
   ```bash
   _run_verifier_ref() {
     local _script="$1" _evidence="$2" _exit
     MINI_ORK_PLAN_PATH="$PLAN_PATH" ARTIFACT_PATH="$ARTIFACT_PATH" \
       bash "$_script" > "$_evidence" 2>&1
     _exit=$?
     # If stdout is JSON with a .pass field, honor it.
     if python3 -c "
   import json,sys
   try:
     d=json.load(open('$_evidence'))
   except Exception:
     sys.exit(2)
   sys.exit(0 if d.get('pass') is True else 1)
   " 2>/dev/null; then
       return 0
     fi
     local _py=$?
     # _py == 2 → not JSON, fall back to exit code (legacy verifiers).
     if [ "$_py" -eq 2 ]; then
       return "$_exit"
     fi
     # _py == 1 → JSON parsed and pass != true.
     return 1
   }
   ```
2. Replace the inline `if ... bash "$_verifier_script" > "$_evidence_path" 2>&1; then` block at `bin/mini-ork-execute:579` with `if _run_verifier_ref "$_verifier_script" "$_evidence_path"; then`.
3. Once the adapter is live and `tests/unit/test_verifier_ref_json.sh` passes, queue removal of the `bin/mini-ork-self-improve:229-260` manual workaround as a follow-up (do NOT remove in the same patch).

**Regression test.** New file `tests/unit/test_verifier_ref_json.sh` with at minimum these bats-style assertions:

- "json verifier with `pass=false` is rejected" — fixture script `echo '{"pass": false}'; exit 0` must cause `_run_verifier_ref` to return non-zero.
- "json verifier with `pass=true` is accepted" — fixture `echo '{"pass": true}'; exit 0` must return 0.
- "legacy verifier with exit 1 and non-JSON stdout is still rejected" — fixture `echo fail; exit 1` must return non-zero.
- "legacy verifier with exit 0 and non-JSON stdout is accepted" — fixture `echo ok; exit 0` must return 0.

**Verification.**
- Existing must pass: `bash bin/mini-ork-self-improve` happy-path smoke (the outer runner's manual JSON parse still succeeds because the adapter returns the same boolean); `tests/unit/test_circuit_breaker.sh` (no overlap with verifier dispatch); any existing `tests/e2e/*` that currently relies on exit-0 legacy verifiers.
- Benchmark delta expected: no measurable wall-time change (the python3 fork already happens in the outer runner; this patch moves it earlier in the call stack). If any p95 regression > 50ms/node appears, treat as a Patch-1 rollback trigger.

**Rollback criteria.**
- If any existing recipe's verifier emits non-JSON stdout that happens to contain the substring `"pass"`, the python3 parse may succeed and the boolean defaults to `None` → false → rejected. Mitigation: the adapter only honors `.pass` when `json.load` succeeds AND `d.get('pass') is True`; non-JSON falls through. If a regression is still observed, revert the dispatch-site change and re-open `learning_record.id=1` with the new failure mode.
- If `tests/unit/test_verifier_ref_json.sh` itself fails on CI after the adapter lands, the dispatcher change must be reverted in the same commit (do not ship partial).

## Lower-ranked patches

### Patch 2: rename `traces` → `execution_traces` in bottleneck-scan prompt

**Problem.** `recipes/recursive-self-improve/prompts/bottleneck-scan.md:13-15` instructs scanners to inspect a `traces` table that does not exist. Every consumer uses `execution_traces` (`lib/trace_store.sh:2`, `lib/context_assembler.sh:87-98`, schema at `db/migrations/0010_benchmarks.sql:12`).

**Change.** Edit `recipes/recursive-self-improve/prompts/bottleneck-scan.md` line 13-15: replace the word `traces` in the "Key tables" line with `execution_traces`. Also audit `bottleneck-scan.md:25-27` and adjacent prompt paragraphs for the same drift.

**Regression test.** Add a bats assertion: `grep -w "execution_traces" recipes/recursive-self-improve/prompts/bottleneck-scan.md` exits 0, AND `grep -wE "^Key tables:.*[^_]traces($|[^_])" recipes/recursive-self-improve/prompts/bottleneck-scan.md` exits non-zero.

**Verification.** Diff is a single-line prompt change. No code paths affected. Next iter's bottleneck scanner sees the live table name.

**Rollback criteria.** None expected; revert only if a future migration renames the table back to `traces`.

### Patch 3: populate `duration_ms` from `task_runs.started_at`/`ended_at`

**Problem.** `_trace_write_node_rich` at `bin/mini-ork-execute:294-308` composes a payload with `cost_usd`, `tool_calls`, `files_read`, `files_written`, `verifier_output`, `reviewer_verdict`, `final_artifact_ref` — but no `duration_ms`. `lib/trace_store.sh:77` defaults the missing field to 0. Result: 40/40 cost-bearing rows in the live `execution_traces` have `duration_ms=0`, breaking p95 and budget gating.

**Change.** Either (a) extend the python3 payload block at `bin/mini-ork-execute:268-308` to accept two new bash args `_started_at` / `_ended_at` and emit `'duration_ms': int((float(ended_at)-float(started_at))*1000)`, OR (b) stash both timestamps in env at `_d021_set_status` (`bin/mini-ork-execute:321-339`) when the terminal status is set, then read them from env in `_trace_write_node_rich`. Option (b) is cleaner because it leaves call sites at `bin/mini-ork-execute:451,472,538` untouched.

**Regression test.** New benchmark task `duration-telemetry-bench` (or extension of `tests/e2e/test_e2e_benchmark_run.sh`) that runs a 4-lens iteration and asserts `sqlite3 .mini-ork/state.db "SELECT COUNT(*) FROM execution_traces WHERE cost_usd > 0 AND duration_ms = 0;"` returns 0.

**Verification.** Schema already accepts the column. Cost: ~8-15 LOC. Risk very low (additive). Enables every future perf patch including Patch 5 to be measured.

**Rollback criteria.** If `duration_ms` ever exceeds 24h (clock skew or env contamination), default to 0 and emit a warning to stderr — do not block the dispatch.

### Patch 4: extend wrapper-pollution check to all durable lens artifacts

**Problem.** `recipes/recursive-self-improve/verifiers/bottlenecks-found.sh:48-50` rejects the z-insight envelope and the star-insight banner only in `$SYNTH`. Prior `self-improve-iter-1-20260609054721/lens-arch.md:10-55` contains a leaked z-insight block that passed the verifier. The bottleneck-scan prompt itself classifies leaked wrappers in durable artifacts as correctness failures (`prompts/bottleneck-scan.md:25-27`).

**Change.** In `bottlenecks-found.sh:48-50`, replace the single-file grep with a loop over `lens-bottleneck.md`, `lens-perf.md`, `lens-correctness.md`, `lens-arch.md`, `lens-arxiv.md`, and `synthesis.md`. Anchor the regex at line-start (e.g. `^[<]z-insight[>]` or equivalent) to avoid false positives on quoted examples inside backticks.

**Regression test.** Add bats case that creates a fixture `${RUN_DIR}/lens-bottleneck.md` containing a leaked z-insight envelope at line-start and asserts the verifier emits `{"pass": false, ...}` with the polluted file in `missing[]`.

**Verification.** Verifier diff < 20 lines. No external dependencies. Catches the exact contamination class arXiv 2604.27586 identifies.

**Rollback criteria.** If a legitimate lens artifact intentionally embeds the envelope tag as a quoted example (no current evidence), narrow the regex further to require start-of-file or otherwise scope by section.

### Patch 5: TTL-bounded env cache for cost-circuit check

**Problem.** `lib/llm-dispatch.sh:348-368` forks `python3` twice on every `llm_dispatch` to aggregate `task_runs.cost_usd` and compare against `MO_DAILY_BUDGET_USD`. At 100K dispatches/day that's 200K forks/day with ~50-80ms startup each, i.e. 2.7-4.5 hours/day pure overhead. The same file at `lib/llm-dispatch.sh:373-414` already documents and fixes the same anti-pattern with an env cache for lane resolution.

**Change.** Mirror the `_MO_LANE_<UPPER>` env export pattern: cache `(spent_today, last_checked_at)` in `_MO_COST_CIRCUIT_SPENT` and `_MO_COST_CIRCUIT_TS`, keyed on `MINI_ORK_DB`, TTL bounded at 30s. Re-read from SQLite only when the TTL is exceeded or the key changes.

**Regression test.** New `tests/perf/test_cost_circuit_cache.sh` that issues 50 `llm_dispatch` calls under `MO_DAILY_BUDGET_USD=50` and uses `strace -c -e trace=clone` (or fork count via `/proc`) to assert ≤ 1 python3 fork for cost-check across the batch.

**Verification.** Prior lens already quantified candidate at `self-improve-iter-1-20260609054721/lens-perf.md:35-49`. Needs Patch 3 (`duration_ms`) first to measure the actual p95 win. Defer until iter 2 unless the wall-time probe in `lens-perf.md:107-121` confirms ≥ 30% reduction.

**Rollback criteria.** Daily budget overshoot above 1.5× (the existing safety margin at `MO_DAILY_BUDGET_USD=50` over typical ~$0.50-2.00/iter cost). If overshoot detected, drop TTL to 0 (effective disable) before reverting.

## Convergence assessment

Not yet converged. Iter 1 surfaces 7 distinct bottlenecks; 2 are *carried forward unfixed* from `self-improve-iter-1-20260609054721`:

- Verifier JSON adapter (this synthesis's Patch 1) — `learning_record.id=1` still `deferred`.
- Cost-circuit cache (this synthesis's Patch 5) — flagged in prior `lens-perf.md:35-49`, not actioned.

The fact that the previous iter's synthesis primarily preserved a deferred patch (commit `ec748c0`) rather than landing it indicates the outer loop is not yet auto-converging on its own correctness blockers. The arch lens explicitly proposes no new infrastructure — refactors only — which is the right shape for this stage but means returns will continue to compound for several iterations before diminishing. **Recommend continuing past iter 1.** Re-evaluate convergence after iter 3 lands Patches 1, 2, 3 and the `duration_ms` telemetry is observable.

## Provenance footer

- Lenses consumed: minimax (`lens-perf.md`), kimi (`lens-correctness.md`), codex (`lens-arch.md`, `lens-bottleneck.md`, `lens-arxiv.md`).
- Synthesizer family: opus.
- arXiv papers cited: 6 (2605.17998, 2602.10133, 2601.06112, 2503.13657, 2601.06007, 2604.27586). All present in `lens-arxiv.md`. No invented references.
- Cross-iteration learnings applied: 1 row from `learning_record` (id=1, deferred verifier adapter — drives Patch 1 ranking). 0 rows from `pattern_records` (frequency ≥ 2 set empty per `lens-bottleneck.md:19`). Prior synthesis degradation pattern from `self-improve-iter-1-20260609054721/synthesis.md:8-20` drives Patch 4 ranking and the convergence verdict above.
