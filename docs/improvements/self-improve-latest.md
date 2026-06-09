# Synthesis — Recursive Self-Improvement, iter 2

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1 | JSON verifier verdicts not authoritative in generic executor (carried from iter 1, still deferred) | correctness | Add `_run_verifier_ref <script> <evidence_path>` helper in `bin/mini-ork-execute`; run verifier, parse evidence-file JSON, honor `.pass` (true ⇒ 0, false ⇒ 1, non-JSON ⇒ fall through to exit code) | `lens-bottleneck.md:7` (Row 1); `lens-correctness.md:18-22,44-64,124-131`; `bin/mini-ork-execute:579`; `recipes/recursive-self-improve/verifiers/bottlenecks-found.sh:9`, `self-tests-pass.sh:10`, `no-regression.sh:11`; `bin/mini-ork-self-improve:229-260` (workaround still in place); `learning_record.id=1` `outcome=deferred`; arXiv 2603.18096 (Paduraru 2026, trace-based assurance contracts), 2602.22302 (Bhardwaj 2026, agent behavioral contracts) — both in `lens-arxiv.md:8-20` | 0.88 |
| 2 | Bottleneck-scan prompt still names non-existent `traces` table (carried from iter 1, not landed) | correctness | Edit `recipes/recursive-self-improve/prompts/bottleneck-scan.md:14` to read `Key tables: execution_traces, benchmark_results, pattern_records, learning_record`; audit the rest of the file for bare `traces` references | `lens-bottleneck.md:9` (Row 3); `lens-correctness.md:24-28,66-77,133-137`; live `.tables` grep — no `traces`, only `execution_traces`; `lib/trace_store.sh:52`, `lib/context_assembler.sh:91` | 0.95 |
| 3 | Wrapper-pollution check ignores non-synthesis lens artifacts | correctness | In `recipes/recursive-self-improve/verifiers/bottlenecks-found.sh:48-50`, replace the single-file grep with a loop over every required durable artifact (`lens-bottleneck.md`, `lens-perf.md`, `lens-correctness.md`, `lens-arch.md`, `lens-arxiv.md`, `synthesis.md`); anchor regex at line-start | `lens-bottleneck.md:10` (Row 4); `lens-correctness.md:30-34,80-106,139-143`; current run's `lens-arch.md` and `lens-arxiv.md` (run-dir copies) contain `<z-insight>` blocks; `tests/integration/test_recursive_self_improve_recipe.sh:148-166`; arXiv 2602.13477 (Naik 2026, orchestrator multi-agent leakage), 2502.12630 (Sternak 2025, prompt-leakage agentic probes) — `lens-arxiv.md:54-69` | 0.85 |
| 4 | `execution_traces.duration_ms` unusable — 10/12 cost-bearing rows are 0 (carried, blocks runtime gating) | correctness/perf | Capture per-node wall-clock at the 3 dispatch call sites (`bin/mini-ork-execute:451,472,538`) and add `duration_ms` to the JSON payload at `bin/mini-ork-execute:294-308`; `lib/trace_store.sh:77` already accepts the key | `lens-bottleneck.md:8` (Row 2); `lens-perf.md:20-52,88-123`; `lens-correctness.md:158-159` open question; recipe gate `recipes/recursive-self-improve/task_class.yaml:38`; arXiv 2604.23853 (Yuan 2026, ClawTrace cost-aware tracing), 2602.10133 (AlSayyad 2026, AgentTrace structured logging) — `lens-arxiv.md:30-44` | 0.82 |
| 5 | Cost-circuit budget check forks Python twice per dispatch with no caching | perf | Mirror the `_MO_LANE_<UPPER>` env-cache pattern (`lib/llm-dispatch.sh:373-414`) for the cost circuit at `:348-368`; cache `_MO_SPENT_TODAY` + `_MO_SPENT_CUTOFF` with 30-60s TTL keyed on `MINI_ORK_DB` | `lens-bottleneck.md:12` (Row 6); `lens-perf.md:54-84,125-167`; arXiv 2601.06007 (Lumer 2026, prompt caching for long-horizon agents), 2510.16276 (Bian 2025, agentic system efficiency) — `lens-arxiv.md:79-91` | 0.74 |

## Top patch — detailed plan

### Patch 1: JSON-aware verifier adapter in `bin/mini-ork-execute`

**Problem statement.** Every recursive-self-improve verifier emits `{"pass": ...}` JSON to its evidence file with `exit 0`, but `bin/mini-ork-execute:579` gates `verifier_ref` only on shell exit. A verifier that detects a real failure and writes `{"pass": false}` still passes. The outer runner `bin/mini-ork-self-improve:229-260` reparses `verifier-result-*.json` with `jq` as a workaround, but the generic executor remains the source of truth for any future recipe.

**Evidence.**
- Internal: `bin/mini-ork-execute:579` (exit-only gate); `recipes/recursive-self-improve/verifiers/bottlenecks-found.sh:9`, `self-tests-pass.sh:10`, `no-regression.sh:11` (all `exit 0` by contract; emit `.pass` in JSON at `bottlenecks-found.sh:62-73`, `self-tests-pass.sh:79-90`, `no-regression.sh:78-93`); `bin/mini-ork-self-improve:229-260` (workaround).
- Reproduction recipe R1 from `lens-correctness.md:45-64` (fake verifier with `pass=false; exit 0` is accepted by the executor).
- Cross-iter: `learning_record.id=1`, category `arch`, `outcome=deferred`, `confidence=0.75`. Iter 1's synthesis ranked this Patch 1 (`self-improve-iter-1-20260609061529/synthesis.md:15`); commit `ec748c0` preserved the deferred row but did not land the adapter.
- arXiv:
  - **2603.18096** (Paduraru 2026, "A Trace-Based Assurance Framework for Agentic AI Orchestration") — explicitly argues verifier observations are structured contract records, not transport exit codes. Confidence 0.78 in `lens-arxiv.md:8-13`.
  - **2602.22302** (Bhardwaj 2026, "Agent Behavioral Contracts") — runtime-enforceable contract pass/fail object consumed before the workflow advances. Confidence 0.70 in `lens-arxiv.md:15-20`.

**Proposed change.**

1. Add `_run_verifier_ref` near `bin/mini-ork-execute:575` (before the existing dispatch site). Sketch:
   ```bash
   _run_verifier_ref() {
     local _script="$1" _evidence="$2" _exit _verdict
     MINI_ORK_PLAN_PATH="$PLAN_PATH" ARTIFACT_PATH="$ARTIFACT_PATH" \
       bash "$_script" > "$_evidence" 2>&1
     _exit=$?
     _verdict="$(python3 - "$_evidence" <<'PY' 2>/dev/null
   import json,sys
   try:
     d=json.load(open(sys.argv[1]))
   except Exception:
     print("nonjson"); sys.exit(0)
   print("pass" if d.get("pass") is True else "fail")
   PY
   )"
     case "$_verdict" in
       pass)    return 0 ;;
       fail)    return 1 ;;
       nonjson|"") return "$_exit" ;;
     esac
   }
   ```
2. Replace the inline `if bash "$_verifier_script" > "$_evidence_path" 2>&1; then` block at `bin/mini-ork-execute:579` with `if _run_verifier_ref "$_verifier_script" "$_evidence_path"; then`.
3. Keep `bin/mini-ork-self-improve:229-260` workaround in place — schedule its removal as a follow-up after one green outer-loop run.

**Regression test.** New `tests/unit/test_verifier_ref_json.sh` with four bats-style assertions (matches `lens-correctness.md:125-129`):
- `echo '{"pass": false}'; exit 0` ⇒ `_run_verifier_ref` returns non-zero.
- `echo '{"pass": true}'; exit 0` ⇒ returns 0.
- `echo 'not json'; exit 1` ⇒ returns non-zero (legacy exit-code gate).
- `echo 'not json'; exit 0` ⇒ returns 0 (legacy exit-code gate).

**Verification.**
- Existing must pass: outer-loop happy path (`bin/mini-ork-self-improve` smoke); `tests/unit/test_circuit_breaker.sh`; existing `tests/e2e/*` relying on exit-0 legacy verifiers.
- Benchmark delta: no expected wall-time change (the python3 fork already happens in the outer runner; this patch shifts it earlier). Treat any p95 regression > 50 ms/node as a rollback trigger.

**Rollback criteria.**
- If a legacy verifier emits non-JSON stdout that nonetheless `json.load`-s (extremely unlikely — would require valid JSON without `.pass`), `_verdict` will be `fail` and the dispatch will reject. Mitigation already in code: only `True` literal honors pass; absent key falls to `fail`. If observed in CI, revert the dispatch-site swap and re-open `learning_record.id=1` with the failure mode.
- If `tests/unit/test_verifier_ref_json.sh` fails on CI, the dispatcher change must be reverted in the same commit — do not ship a partial.

## Lower-ranked patches

### Patch 2: rename `traces` → `execution_traces` in bottleneck-scan prompt

**Problem.** `recipes/recursive-self-improve/prompts/bottleneck-scan.md:14` reads `Key tables: traces, ...`. Live schema has no `traces`; every consumer uses `execution_traces` (`lib/trace_store.sh:52`, `lib/context_assembler.sh:91`, `db/migrations/0010_benchmarks.sql:12`).

**Change.** Edit `bottleneck-scan.md:14` to `Key tables: execution_traces, benchmark_results, pattern_records, learning_record`. Audit `bottleneck-scan.md:25-27` and adjacent paragraphs for residual bare `traces`.

**Regression test.** Bats assertion: `grep -w "execution_traces" recipes/recursive-self-improve/prompts/bottleneck-scan.md` exits 0 AND `grep -wE "^[[:space:]]*Key tables:.*[^_]traces($|[^_])" recipes/recursive-self-improve/prompts/bottleneck-scan.md` exits non-zero.

**Verification.** Single-line prompt edit. No code paths affected. Next iter's scanner sees the live table name.

**Rollback criteria.** None expected; revert only if a future migration renames the table back to `traces`.

### Patch 3: extend wrapper-pollution check to all durable lens artifacts

**Problem.** `recipes/recursive-self-improve/verifiers/bottlenecks-found.sh:48-50` rejects `<z-insight>` / `★ Insight` only in `$SYNTH`. This iteration's `lens-arch.md` and `lens-arxiv.md` (run-dir copies, written via stdout of the executor) contain full `<z-insight>` envelopes — the verifier passes them. Prior `self-improve-iter-1-20260609054721/lens-arch.md:10-55` shipped the same pollution. Integration test `tests/integration/test_recursive_self_improve_recipe.sh:148-166` only covers polluted `synthesis.md`.

**Change.** In `bottlenecks-found.sh:48-50`, replace the single grep with a loop:
```bash
for _f in lens-bottleneck.md lens-perf.md lens-correctness.md lens-arch.md lens-arxiv.md synthesis.md; do
  [ -f "$RUN_DIR/$_f" ] || continue
  if grep -qE '^(<z-insight>|★ Insight)' "$RUN_DIR/$_f"; then
    missing+=("$_f: leaked envelope")
    pass=false
  fi
done
```
Anchor regex at line-start (`^`) to avoid false positives on quoted examples inside fenced code blocks.

**Regression test.** Extend `tests/integration/test_recursive_self_improve_recipe.sh:148-166` with a fixture `lens-bottleneck.md` containing `<z-insight>` at line-start; assert verifier emits `{"pass": false, ...}` with the polluted file in `missing[]`. Reproduction R3 in `lens-correctness.md:82-106`.

**Verification.** Verifier diff < 20 LoC. No external dependencies. Existing integration test continues to pass.

**Rollback criteria.** If a legitimate lens intentionally quotes the envelope tag inside a fenced block (no current evidence), narrow regex to require the tag at start-of-file or constrain to a specific section header.

### Patch 4: populate `duration_ms` for every node trace

**Problem.** `_trace_write_node_rich` at `bin/mini-ork-execute:294-308` composes a JSON payload without a `duration_ms` field. `lib/trace_store.sh:76-77` defaults the missing key to 0. Live DB: 10 of 12 cost-bearing rows have `duration_ms=0` (e.g. `tr-implementer-1780986658-59964 cost_usd=1.43 duration_ms=0`). Recipe gate `recipes/recursive-self-improve/task_class.yaml:38` (`runtime_model.max_minutes: 60`) cannot be enforced.

**Change.** Two equivalent options — pick (a) for the smaller diff:
- (a) At each of the three dispatch call sites (`bin/mini-ork-execute:451,472,538`) capture `_t0` before `llm_dispatch` and pass `$(( $(_now_ms) - _t0 ))` as a new positional arg to `_trace_write_node_rich`. Add `'duration_ms': int(sys.argv[N])` to the python heredoc at `:294-308`. Implement `_now_ms` as `python3 -c 'import time; print(int(time.time()*1000))'` for macOS portability (one Python fork is acceptable here; the perf-sensitive path is the cost circuit, addressed by Patch 5).
- (b) Stash `_started_at` in env at `_d021_set_status` (`bin/mini-ork-execute:321-339`) when the terminal status is set and read it back in `_trace_write_node_rich`. Cleaner separation but more env coupling.

**Regression test.** New `tests/unit/test_trace_duration.sh`: stub a no-op node that sleeps ~200 ms, assert `SELECT duration_ms FROM execution_traces WHERE trace_id=?` returns a value in `[150, 600]`. Negative assertion: after the patch, `SELECT COUNT(*) FROM execution_traces WHERE cost_usd > 0 AND duration_ms = 0` returns 0 on a fresh run.

**Verification.** Schema already accepts the column. Cost: ~10–15 LoC. Risk class low (additive). Enables future perf measurement (Patch 5's gain becomes observable) and the `task_class.yaml` runtime gate.

**Rollback criteria.** If a `duration_ms` exceeds 24 h (clock skew / env contamination), clamp to 0 and emit a stderr warning rather than block dispatch.

### Patch 5: TTL-bounded env cache for cost-circuit check

**Problem.** `lib/llm-dispatch.sh:348-368` forks `python3` twice on every dispatch — one heredoc for `sqlite3` SUM, one for the float compare. The same file already implements the same anti-pattern fix for lane resolution at `:373-414`. At 100 dispatches/iter × ~25–50 ms/fork: ~3–5 s wasted per iteration.

**Change.** Mirror `_MO_LANE_<UPPER>`: introduce `_MO_SPENT_TODAY` and `_MO_SPENT_CUTOFF` env vars keyed on `MINI_ORK_DB`, TTL 30–60 s, refreshed on miss. Fold the float compare into the same heredoc (Patch F3 from `lens-perf.md:169-191` is a strict subset of this).

**Regression test.** `tests/perf/test_cost_circuit_cache.sh`: 10 `llm_dispatch` calls under `MO_DAILY_BUDGET_USD=50`, count `python3` invocations via `strace -e trace=execve -f` (or shell trace). Cold: 1. Cached: 0 for the read until TTL.

**Verification.** Requires Patch 4 (`duration_ms`) to land first so the wall-time win is actually observable in `execution_traces`. Defer if not landed; keep on the rank list.

**Rollback criteria.** Daily-budget overshoot above 1.5× (existing safety margin). If observed, drop TTL to 0 (effective disable) before reverting.

## Convergence assessment

**Not yet converged.** Iter 2 reproduces every iter-1 finding plus surfaces two architecture rows (substring synth routing, provider-policy split-brain) that the arch lens explicitly defers as "no new infra required this iteration." The most telling signal: `learning_record` has 1 deferred row and 0 resolved rows, and `pattern_records.frequency >= 2` is empty — the outer loop is not yet recording resolutions. Patches 1 and 2 from iter 1 did not land (grep confirms `_run_verifier_ref` absent from `bin/mini-ork-execute`; `bottleneck-scan.md:14` still says `traces`). The architecture lens's two candidates (`artifact_role` workflow field, `MINI_ORK_PROVIDER_POLICY` first-class env) are queued for a future iteration but are unranked here because Patch 1 is the binding constraint — until verifier verdicts are authoritative, no other patch can be reliably gated.

**Recommendation:** continue past iter 2. Re-evaluate convergence after Patches 1–3 have actually landed (one resolved `learning_record` row + one populated `pattern_records` row at frequency ≥ 2 would be the signal). The outer runner should treat a second consecutive iteration where `learning_record.id=1` remains `deferred` as a hard escalation event and refuse to advance until the JSON adapter lands.

## Provenance footer

- Lenses consumed: minimax (`lens-perf.md`), kimi (`lens-correctness.md`), codex (`lens-arch.md`), arXiv (`lens-arxiv.md`), bottleneck scan (`lens-bottleneck.md`)
- Synthesizer family: opus
- arXiv papers cited: 7 (2603.18096, 2602.22302, 2604.23853, 2602.10133, 2602.13477, 2502.12630, 2601.06007); 2510.16276 cited as secondary
- Cross-iteration learnings applied: 1 row from `learning_record` (id=1, deferred); 5 carried items from `self-improve-iter-1-20260609061529/synthesis.md` (verifier adapter, traces rename, duration_ms, wrapper pollution, cost-circuit cache)
