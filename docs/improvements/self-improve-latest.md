# Synthesis — Recursive Self-Improvement, iter 3

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1 | `learning_record.id=1` stays `deferred` even though `_run_verifier_ref` landed in iter 2; every subsequent scan re-ranks resolved work | correctness (meta) | Close the row (`outcome=resolved`), correct `docs/improvements/self-improve-latest.md:7,17-22`, and add a minimal promotion hook in `bin/mini-ork-self-improve` that flips matching `learning_record` rows to `resolved` when the row's associated regression test passes; ship with a unit test that asserts a deferred row transitions on a green run | `lens-bottleneck.md:10` (Row 4); `lens-correctness.md:7,15,25-34,99-102,134`; `lens-arch.md:15`; `bin/mini-ork-execute:68-107` (adapter exists); `bin/mini-ork-execute:624` (dispatched); `tests/unit/test_verifier_ref_json.sh:61-64` (already green); `learning_record` SQLite row 1; iter 2 ranking placed adapter at Rank 1 (`self-improve-iter-2-20260609064419/synthesis.md:7`); arXiv 2605.17998 (Nguyen 2026, verify-gated completion → admission close) and 2605.20312 (Kadaboina 2026, replayable verification artifacts) — `lens-arxiv.md:32-44` | 0.90 |
| 2 | Bottleneck-scan prompt names non-existent `traces` table; scanners that follow the prompt query a missing table | correctness | One-line edit at `recipes/recursive-self-improve/prompts/bottleneck-scan.md:14` to read `Key tables: execution_traces, benchmark_results, pattern_records, learning_record`; audit file for residual bare `traces` references | `lens-bottleneck.md:8` (Row 2); `lens-correctness.md:9,52-62,111-116`; live schema `db/migrations/0010_benchmarks.sql:12`; consumer `lib/context_assembler.sh:91-95`; carried from iter 1 and iter 2 (`self-improve-iter-2-20260609064419/synthesis.md:9`); no arXiv required (prose-rename) | 0.96 |
| 3 | Wrapper-pollution check only greps `synthesis.md` while every other durable lens artifact can leak CLI envelope blocks (the z-insight JSON tag and the Insight-rule banner) | correctness | Expand `recipes/recursive-self-improve/verifiers/bottlenecks-found.sh:48-50` to loop over every durable artifact declared in `artifact_contract.yaml` (or hard-coded list: `lens-bottleneck.md`, `lens-perf.md`, `lens-correctness.md`, `lens-arch.md`, `lens-arxiv.md`, `synthesis.md`); anchor the regex at line-start | `lens-bottleneck.md:9` (Row 3); `lens-correctness.md:8,36-50,104-109`; live evidence — current iter-3 run-dir `lens-bottleneck.md:7-52`, `lens-arch.md:9-54`, `lens-arxiv.md:12-57` all carry the CLI envelope block at line start; carried from iter 2 (`self-improve-iter-2-20260609064419/synthesis.md:9`); arXiv 2604.01350 (Yang 2026, shared-state contamination) and 2605.16746 (Wang 2026, memory laundering) — `lens-arxiv.md:55-68` | 0.86 |
| 4 | `execution_traces.duration_ms` always 0 for cost-bearing rows; recipe `task_class.yaml:38-40 max_minutes:60` gate is unenforceable | correctness/perf | At each of the 3 dispatch call sites in `bin/mini-ork-execute` capture `_t0` before `llm_dispatch` and pass elapsed millis as a positional arg to `_trace_write_node_rich`; extend the Python payload dict at `:315-354` with `'duration_ms': int(sys.argv[N])`; schema already accepts the column | `lens-bottleneck.md:7` (Row 1); `lens-perf.md:7-15,32-58,86-123`; `lens-correctness.md:10,64-77,118-123`; live DB: 13/15 cost-bearing rows have `duration_ms=0`; carried from iter 1 and iter 2 (`self-improve-iter-2-20260609064419/synthesis.md:10`); arXiv 2604.05119 (Pathak 2026, governance-aware telemetry), 2601.08815 (Ye 2026, resource-bounded contracts), 2604.23853 (Yuan 2026, ClawTrace) — `lens-arxiv.md:7-28` | 0.84 |
| 5 | `no-regression.sh` emits benchmark summary but pass/fail is decided only by shell-syntax and implementer report — a 50% utility drop still passes | correctness | After `bench_summary` is computed in `recipes/recursive-self-improve/verifiers/no-regression.sh:38-60`, look up the prior baseline `utility_score` for the same `task_class` and set `bench_delta_ok=0` if `utility_delta < -0.05` (configurable); thread it into the existing pass condition at `:72-76` | `lens-bottleneck.md:11` (Row 5); `lens-correctness.md:11,79-93,125-130`; live baseline data: stale synthetic `br-wc-3a202-refactor-*` from 2026-06-02; arXiv 2604.10547 (Chen 2026, Agent² RL-Bench) and 2604.00072 (Scrivens 2026, classification vs verification gates) — `lens-arxiv.md:79-92` | 0.72 |

## Top patch — detailed plan

### Patch 1: close `learning_record.id=1` and add a state-closure promotion hook

**Problem statement.** Iter 2's Rank-1 patch — the `_run_verifier_ref` JSON adapter — actually landed in code (`bin/mini-ork-execute:68-107`, dispatched at `:624`, with a green regression test at `tests/unit/test_verifier_ref_json.sh:61-64`), but the `learning_record.id=1` row that tracked it still reads `outcome=deferred`. Every subsequent bottleneck scan therefore re-ranks the same already-resolved work, the synthesizer's cross-iteration memory drifts, and convergence is structurally impossible. Iter 2 (`self-improve-iter-2-20260609064419/synthesis.md:7`) explicitly flagged this exact scenario as an escalation event. Root cause: there is no automated promotion path from "regression test passes + code present" to "row resolved". Fixing the row by hand without a hook only papers over the same drift next iteration.

**Evidence.**
- mini-ork internal:
  - Adapter exists: `bin/mini-ork-execute:68-107` (function `_run_verifier_ref`).
  - Adapter is dispatched on the verifier path: `bin/mini-ork-execute:624`.
  - Regression test green: `tests/unit/test_verifier_ref_json.sh:61-64`.
  - Stale record: `learning_record.id=1` → `outcome=deferred`, `category=arch`, `title="Verifier verdict JSON adapter (_run_verifier_ref)"`.
  - Stale docs: `docs/improvements/self-improve-latest.md:7,17-22` still describe the adapter as absent/deferred.
  - Iter-2 synthesizer flagged this as the convergence-blocking signal: `self-improve-iter-2-20260609064419/synthesis.md:115-127` (convergence assessment).
- Cross-lens:
  - `lens-bottleneck.md:10` (Row 4 — "Learning/state closure is stale").
  - `lens-correctness.md:7,15` (B1 silent corruption taxonomy) + reproduction `lens-correctness.md:25-34`.
  - `lens-arch.md:15` (architecture-adjacent state-management gap; deliberately not promoted to primary arch refactor candidate).
- arXiv (mandatory because the patch adds a new `bin/mini-ork-promote` style hook — minor new infra):
  - **2605.17998** (Nguyen 2026, verify-gated completion as admission control) — `lens-arxiv.md:32-37`, confidence 0.82. Direct support for separating completion-proposal from completion-admission: once the verifier verdict exists, the learning state must transition out of "proposed/deferred" — admission is not optional bookkeeping.
  - **2605.20312** (Kadaboina 2026, replayable claim-verification artifacts) — `lens-arxiv.md:39-44`, confidence 0.71. Supports recording the closure with stable evidence paths (`bin/mini-ork-execute:68-107`, `tests/unit/test_verifier_ref_json.sh:61-64`) so future scans can verify the closure rather than re-derive it.

**Proposed change.** Three small, ordered edits in one patch:

1. **One-shot row closure.** Execute, idempotently, the SQL:
   ```sql
   UPDATE learning_record
     SET outcome='resolved',
         updated_at=strftime('%s','now'),
         evidence=json_set(COALESCE(evidence,'[]'),
           '$[#]', 'tests/unit/test_verifier_ref_json.sh:61-64')
     WHERE id=1 AND outcome='deferred';
   ```
   Wrap in `bin/mini-ork-self-improve` (or a tiny new `bin/mini-ork-promote`) so the next outer-loop run executes it automatically on startup. Keep the wrapper guarded by `id=1` so it only runs the first time.

2. **Docs correction.** Edit `docs/improvements/self-improve-latest.md:7` to remove the "still deferred" framing and replace `:17-22` with one paragraph stating the adapter landed in iter 2, citing `bin/mini-ork-execute:68-107`, `:624`, and `tests/unit/test_verifier_ref_json.sh:61-64`. Add a one-sentence note that the iter-3 self-improve report supersedes this section; the iter-3 report will be written by the publisher node referencing this synthesis.

3. **Promotion hook (the real durable fix).** Add `bin/mini-ork-promote` (or extend `bin/mini-ork-self-improve` startup) with a single function `_promote_resolved_learnings`:

   ```bash
   _promote_resolved_learnings() {
     # For every learning_record row with outcome='deferred' that names a
     # regression test path in its evidence array, run the test; if it
     # exits 0, flip outcome to 'resolved' and append the run timestamp.
     local _db="${MINI_ORK_DB:-.mini-ork/state.db}"
     [[ -f "$_db" ]] || return 0
     local _rows; _rows=$(sqlite3 "$_db" \
       "SELECT id, evidence FROM learning_record WHERE outcome='deferred';")
     while IFS='|' read -r _id _ev; do
       [[ -z "$_id" ]] && continue
       local _test
       _test=$(printf '%s' "$_ev" | python3 -c \
         'import sys,json; print(next((p for p in json.load(sys.stdin) if p.endswith(".sh") and "/test_" in p), ""))')
       [[ -z "$_test" || ! -x "$_test" ]] && continue
       if "$_test" >/dev/null 2>&1; then
         sqlite3 "$_db" \
           "UPDATE learning_record SET outcome='resolved',
              updated_at=strftime('%s','now') WHERE id=$_id;"
       fi
     done <<< "$_rows"
   }
   ```

   Call `_promote_resolved_learnings` from `bin/mini-ork-self-improve` near `:90-95` (right after the policy override is staged, before the recipe is dispatched). This makes every future outer-loop iteration self-healing.

**Regression test.** `tests/unit/test_promote_resolved_learnings.sh`. Assertions:
- Given a fresh test sqlite db with one `learning_record` row whose `outcome='deferred'` and whose `evidence` array contains a path to a known-passing stub test script, calling `_promote_resolved_learnings` flips the row to `outcome='resolved'` and stamps a non-null `updated_at`.
- Given the same setup but the stub test exits 1, the row remains `outcome='deferred'` and `updated_at` is unchanged.
- Given a row whose evidence array has no `test_*.sh` entry, the function leaves the row untouched (no-op safety).

Assertion text the test must contain verbatim:
- `assert_equal "resolved" "$outcome" "deferred row must promote on green test"`
- `assert_equal "deferred" "$outcome" "deferred row must stay on failed test"`
- `assert_equal "deferred" "$outcome" "row without test path must be left alone"`

**Verification.**
- Existing tests that must continue to pass: `tests/unit/test_verifier_ref_json.sh`, the full `tests/unit/` suite (run `make test-unit` or the project equivalent), and `tests/integration/test_recursive_self_improve_recipe.sh`.
- Bottleneck-scan smoke: re-running `bash recipes/recursive-self-improve/prompts/bottleneck-scan.md` (via the next outer iteration) must no longer surface the verifier adapter row as a top-ranked bottleneck. Expected delta: the iter-4 `lens-bottleneck.md` top-ranked table shrinks by one row (`learning_record.id=1` no longer shows as Row 4 / Row 1 in correctness lens).
- DB-side: after one outer-loop run, `sqlite3 "$MINI_ORK_DB" "SELECT outcome FROM learning_record WHERE id=1"` must return `resolved`.
- Sign on convergence indicator: `learning_record` should now contain ≥1 `resolved` row (currently 0). The synthesizer should treat this as the first observable convergence signal.

**Rollback criteria.** Discard this patch and revert all three edits if any of:
- The promotion hook flips a row to `resolved` on a regression test that is itself stale or skipped (e.g. the test exits 0 because it is empty). Mitigation before rollback: gate `_promote_resolved_learnings` on the test producing at least one assertion (e.g. require `grep -q assert_ "$_test"`).
- `_run_verifier_ref` is removed or substantively refactored away in a future iter — re-open `learning_record.id=1` to `outcome=deferred` manually and disable the auto-promotion for that row id.
- The hook materially slows `bin/mini-ork-self-improve` startup (>2 s). If observed, move the promotion check behind an opt-in `MINI_ORK_PROMOTE_LEARNINGS=1` env var.

## Lower-ranked patches

### Patch 2: rename `traces` → `execution_traces` in bottleneck-scan prompt

- **Problem.** `recipes/recursive-self-improve/prompts/bottleneck-scan.md:14` says `Key tables: traces`; the live schema has only `execution_traces` (`db/migrations/0010_benchmarks.sql:12`). A scanner that obeys the prompt queries a missing table and concludes "no perf signals". Carried unfixed from iter 1 and iter 2.
- **Change.** Single-line edit: replace `Key tables: traces, …` with `Key tables: execution_traces, benchmark_results, pattern_records, learning_record`. Audit the rest of the prompt for residual bare `traces` references.
- **Regression test.** `grep -w "execution_traces" recipes/recursive-self-improve/prompts/bottleneck-scan.md` must exit 0 AND `grep -wE "^[[:space:]]*Key tables:.*[^_]traces($|[^_])" recipes/recursive-self-improve/prompts/bottleneck-scan.md` must exit non-zero. Assertion text: `assert_grep "execution_traces" recipes/recursive-self-improve/prompts/bottleneck-scan.md "prompt must name execution_traces"`.
- **Verification.** No code paths read this prompt at runtime in a way that requires migration. Diff < 5 LoC.
- **Rollback.** Revert only if a future migration renames the table back to `traces`.

### Patch 3: extend wrapper-pollution verifier to every durable artifact

- **Problem.** `recipes/recursive-self-improve/verifiers/bottlenecks-found.sh:48-50` greps only `$SYNTH` (synthesis.md) for the z-insight JSON envelope tag and the Insight-rule banner. Current iter-3 run dir already carries those envelope blocks at line start in `lens-bottleneck.md:7`, `lens-arch.md:9`, and `lens-arxiv.md:12`; the verifier passes anyway. Carried from iter 2.
- **Change.** Replace the single-file grep with a loop over an explicit array of durable artifacts (`lens-bottleneck.md lens-perf.md lens-correctness.md lens-arch.md lens-arxiv.md synthesis.md`). Anchor both alternation branches at line-start (`^` applied to both the z-insight tag pattern and the Insight-rule banner pattern — the current regex only anchors the left branch) to avoid false positives where the envelope tokens are legitimately quoted inside prose. Emit each polluted file path into the verifier's existing `missing[]` array so the JSON failure shape stays stable.
- **Regression test.** Extend `tests/integration/test_recursive_self_improve_recipe.sh:166-184` with a fixture polluted `lens-arch.md` (carries the z-insight envelope at line start) and assert verifier emits `pass=false` with `lens-arch.md` in `missing[]`. Assertion text: `assert_equal "false" "$pass" "verifier must reject envelope in any durable artifact"`.
- **Verification.** Diff < 20 LoC. Existing single-file synthesis check continues to fire as a subset of the new loop. arXiv 2604.01350 (Yang 2026), 2605.16746 (Wang 2026) — `lens-arxiv.md:55-68` — support the broader scope.
- **Rollback.** If a legitimate lens intentionally quotes the envelope tag at line-start in a fenced block (no current evidence), narrow the regex to require start-of-file or restrict to a specific section header.

### Patch 4: populate `duration_ms` at the three dispatch call sites

- **Problem.** `_trace_write_node_rich` at `bin/mini-ork-execute:300-358` composes the JSON payload without a `duration_ms` key; `lib/trace_store.sh:77` defaults missing keys to `0`; live data shows 13/15 cost-bearing rows have `duration_ms=0`. Recipe gate `recipes/recursive-self-improve/task_class.yaml:38-40` (`runtime_model.max_minutes: 60`) cannot be enforced. Carried from iter 1 and iter 2.
- **Change.** At each of the 3 dispatch call sites in `bin/mini-ork-execute` (researcher / implementer / reviewer dispatch — see `lens-correctness.md:120`), capture `_t0=$(_now_ms)` before `llm_dispatch` and pass `$(( $(_now_ms) - _t0 ))` as a new positional arg to `_trace_write_node_rich`. Add `'duration_ms': int(sys.argv[N])` to the Python heredoc at `:315-354`. Implement `_now_ms` as `python3 -c 'import time; print(int(time.time()*1000))'` for macOS portability (one fork is acceptable here — perf-sensitive forking is the cost circuit handled by Patch 5, not this trace path).
- **Regression test.** `tests/unit/test_trace_duration.sh`: stub a no-op node that sleeps ~200 ms; assert `SELECT duration_ms FROM execution_traces WHERE trace_id=?` returns a value in `[150, 600]`. Negative assertion after patch: `SELECT COUNT(*) FROM execution_traces WHERE cost_usd > 0 AND duration_ms = 0` returns 0 on a fresh run. Assertion text: `assert_within 150 600 "$dur" "duration_ms must reflect wall-clock"`.
- **Verification.** Schema already accepts the column. Diff ~25-40 LoC. arXiv 2604.05119 (Pathak 2026), 2601.08815 (Ye 2026), 2604.23853 (Yuan 2026) — `lens-arxiv.md:7-28` — support telemetry-as-enforcement.
- **Rollback.** If a `duration_ms` exceeds 24 h (clock skew), clamp to 0 and emit a stderr warning rather than block dispatch.

### Patch 5: teach `no-regression` to actually gate on benchmark regression

- **Problem.** `recipes/recursive-self-improve/verifiers/no-regression.sh:38-60` computes `bench_summary` but the pass/fail decision at `:72-76` only checks shell-syntax failures and the implementer report outcome. A patch that drops `utility_score` by 50% still passes the verifier. Live `benchmark_results` data is stale synthetic (`br-wc-3a202-refactor-*` from 2026-06-02), so this is also a data-freshness sub-issue.
- **Change.** After `bench_summary` is computed, look up the prior baseline `utility_score` for the same `task_class` from `benchmark_results`. If `utility_delta < -0.05` (configurable via `MO_BENCH_REGRESSION_TOL`), set `bench_delta_ok=0`. Fold it into the existing pass condition at `:72-76` as an additional AND term. Document the env knob in `recipes/recursive-self-improve/README.md` (or equivalent).
- **Regression test.** Insert a synthetic benchmark row with `utility_score=0.5` against a baseline of `0.8`; assert verifier returns `pass=false` with `benchmark_regression=true` in the JSON. Assertion text: `assert_equal "true" "$benchmark_regression" "must fail on > 5% utility drop"`.
- **Verification.** arXiv 2604.10547 (Chen 2026, Agent² RL-Bench) and 2604.00072 (Scrivens 2026, gates-vs-classifiers) — `lens-arxiv.md:79-92` — support converting observed deltas into hard gates.
- **Rollback.** If benchmark noise causes >20% false-positive regressions on otherwise-clean runs, widen the tolerance to 0.10 or disable the gate behind `MO_BENCH_REGRESSION_TOL=999`.

## Convergence assessment

**Not yet converged, but the binding signal has shifted.** Iter 2's Rank-1 patch (`_run_verifier_ref` JSON adapter) did land in code — `bin/mini-ork-execute:68-107` confirms it, and `tests/unit/test_verifier_ref_json.sh:61-64` is green. That is real progress: the outer loop produced a functioning patch for the first observable time across iters 1-2. However, the corresponding `learning_record.id=1` row is still `outcome=deferred`, and `docs/improvements/self-improve-latest.md:7,17-22` still describes the adapter as absent. Without state closure, every future scan re-ranks resolved work as if it were open, so the synthesizer cannot tell progress from drift — exactly what iter 2's convergence note warned about ("a second consecutive iteration where `learning_record.id=1` remains `deferred` as a hard escalation event").

The remaining bottlenecks are also clustered into two persistent buckets that iter 1 and iter 2 already named:
- **Bookkeeping / state-closure** (Patches 1, 2): trivially small, repeatedly missed because no one is enforcing closure.
- **Telemetry / verifier scope** (Patches 3, 4, 5): each is < 50 LoC, each carries arXiv support, and the longer they stay open, the more they corrupt the very signal the outer loop depends on.

`pattern_records.frequency >= 2` remains empty, so the loop has still never promoted a recurring pattern. Patch 1 should also restore the substrate for that promotion path (once one `learning_record` row reaches `resolved`, a second deferred-then-resolved row in a future iteration is the minimum needed to promote a pattern).

**Recommendation:** continue past iter 3. The convergence trigger the outer loop should watch for is: `(a)` `learning_record.id=1.outcome='resolved'`, `(b)` iter-4 `lens-bottleneck.md` no longer ranks any of Patches 2-5 in its top 5, `(c)` at least one `pattern_records` row with `frequency >= 2`. Hitting all three is the earliest honest convergence signal; until then, more iterations are warranted.

## Provenance footer

- Lenses consumed: minimax (`lens-perf.md`), kimi (`lens-correctness.md`), codex (`lens-arch.md`), arXiv (`lens-arxiv.md`), bottleneck scan (`lens-bottleneck.md`)
- Synthesizer family: opus
- arXiv papers cited: 10 unique IDs — 2604.05119, 2601.08815, 2604.23853 (Patch 4); 2605.17998, 2605.20312 (Patch 1); 2604.01350, 2605.16746 (Patch 3); 2604.10547, 2604.00072 (Patch 5); 2503.13577 (referenced for cross-cutting routing caution). All resolved in `lens-arxiv.md`; none invented.
- Cross-iteration learnings applied: 1 row from `learning_record` (id=1, deferred — feeds Patch 1 directly). 0 rows from `pattern_records` (table currently has no `frequency >= 2` rows). Prior synthesis context consumed: `self-improve-iter-2-20260609064419/synthesis.md:7-15,100-140` (carried-patch identity and convergence framing) and `self-improve-iter-1-20260609061529/synthesis.md` for original framing of the verifier adapter.
