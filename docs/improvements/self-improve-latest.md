# Synthesis — Recursive Self-Improvement, iter 4

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1 | Learning-state closure cannot promote `deferred` rows to `resolved` (3rd-iter recurrence) | correctness | Flip `learning_record.id=1` to `resolved` and add `_promote_resolved_learnings` startup hook in `bin/mini-ork-self-improve` that runs the test cited in each `deferred` row and promotes on green. | `lens-bottleneck.md:7` (Row 1), `lens-correctness.md:7` (B1), `bin/mini-ork-execute:68`, `bin/mini-ork-execute:624`, `tests/unit/test_verifier_ref_json.sh:61`, prior `docs/improvements/self-improve-latest.md:7` | 0.85 |
| 2 | Bottleneck prompt cites non-existent `traces` table | correctness | Single-line edit at `recipes/recursive-self-improve/prompts/bottleneck-scan.md:14` — `traces` → `execution_traces`. Audit prompt for any other bare `traces` reference. | `lens-bottleneck.md:9` (Row 3), `lens-correctness.md:8` (B2), `db/migrations/0010_benchmarks.sql:12`, prior `docs/improvements/self-improve-latest.md:8` | 0.95 |
| 3 | `_trace_write_node_rich` never writes `duration_ms`; p95 / runtime gates read 0 | perf | Capture `_node_t0_ms` immediately before `RESULT=$(llm_dispatch …)` in researcher/implementer/reviewer branches; pass `duration_ms` as a 7th positional arg to `_trace_write_node_rich`; add `obj['duration_ms']=int(duration_ms)` in the python3 builder. Use a `python3 -c "import time;print(int(time.time()*1000))"` shim for macOS `date` portability. | `lens-bottleneck.md:8` (Row 2), `lens-perf.md:17` (H-1), `bin/mini-ork-execute:300-358`, `bin/mini-ork-execute:441`, `db/migrations/0014_execution_traces_relax_fk_and_status.sql:40` | 0.80 |
| 4 | Pollution scan misses `arxiv-refs.md` fallback and has no regression for the expanded set | correctness | Replace single-file grep in `verifiers/bottlenecks-found.sh:48-65` with a loop over the explicit artifact array (`lens-bottleneck.md`, `lens-perf.md`, `lens-correctness.md`, `lens-arch.md`, `lens-arxiv.md`, `synthesis.md`, `arxiv-refs.md`). Anchor regex at `^` to avoid prose false positives. | `lens-bottleneck.md:10` (Row 4), `lens-correctness.md:9` (B3), `recipes/recursive-self-improve/artifact_contract.yaml:11`, prior commit `e95e641` (loop-found leak) | 0.80 |
| 5 | Cost-circuit forks `python3` + queries SQLite on every dispatch; lane lookup already cached | perf | Add `MO_COST_CIRCUIT_LAST` + `MO_COST_CIRCUIT_LAST_SPENT` env cache with `MO_COST_CIRCUIT_TTL:-15` around `lib/llm-dispatch.sh:348-368`; replace the second `python3` float compare with `awk -v s b 'BEGIN{exit !(s+0>=b+0)}'`; invalidate cache after every `_d022_charge_node_cost` write. | `lens-bottleneck.md:12` (Row 6), `lens-perf.md:23` (H-2), `lib/llm-dispatch.sh:348-368`, `lib/llm-dispatch.sh:373-414` (lane-cache precedent) | 0.75 |

## Top patch — detailed plan

### Patch 1: Close `learning_record.id=1` and add startup promotion hook

**Problem statement.** The `learning_record` row for the JSON verifier adapter
(`id=1`) is still `outcome='deferred'` even though the adapter, its dispatch
site, and a passing regression test all exist. Because no verifier inspects
`learning_record.outcome`, every iteration re-ranks resolved work as open and
the recursive-self-improve loop cannot converge. This has been the rank-1 or
rank-2 finding in iter 1, iter 2, and iter 3 syntheses without ever being
applied by the implementer.

**Evidence.**
- `bin/mini-ork-execute:68` — `_run_verifier_ref` function definition (landed).
- `bin/mini-ork-execute:624` — dispatcher call site for the adapter (landed).
- `tests/unit/test_verifier_ref_json.sh:61` — regression assertion (passing,
  per correctness lens reproduction recipe at `lens-correctness.md:45-47`).
- `lens-correctness.md:7` (Row B1) — DB ground truth: `resolved_count=0`,
  `learning_record.id=1.outcome='deferred'`.
- `lens-bottleneck.md:7` (Row 1) — confirms `learning_record` cannot suppress
  closed findings.
- Prior synthesis: `docs/improvements/self-improve-latest.md:7` (iter 3
  rank 1) — same item, deferred again.
- New infra required? No (table already exists; only data + a bash function
  are added). arXiv citation not required per arch lens guidance.

**Proposed change.**
1. **Idempotent SQL backfill** in `bin/mini-ork-self-improve` near the staging
   block at `:90-98`:
   ```sql
   UPDATE learning_record
   SET outcome = 'resolved', resolved_at = strftime('%s','now')
   WHERE id = 1 AND outcome = 'deferred';
   ```
   Run via `sqlite3 "$MINI_ORK_DB"` guarded by a `[[ -f "$MINI_ORK_DB" ]]`
   check so a fresh-clone run does not fail.
2. **`_promote_resolved_learnings()` bash function** in
   `bin/mini-ork-self-improve` (insert above the `_stage_provider_policy`
   block at `:90`). For each row in
   `SELECT id, evidence_paths FROM learning_record WHERE outcome='deferred'`:
   - Parse `evidence_paths` JSON (use `python3 -c "import json,sys;…"` to
     stay consistent with existing tool use).
   - Extract entries matching `tests/.*\.sh$`.
   - For each test path that exists AND contains at least one `assert_`
     call (`grep -q '^assert_\|[^A-Za-z]assert_' "$_test"`), run it with
     `bash "$_test"`; if exit 0, flip the row:
     ```sql
     UPDATE learning_record SET outcome='resolved',
       resolved_at=strftime('%s','now') WHERE id=:id AND outcome='deferred';
     ```
   - Gate the whole function behind `MINI_ORK_PROMOTE_LEARNINGS=${MINI_ORK_PROMOTE_LEARNINGS:-1}`
     so it can be disabled if startup latency regresses.
3. **Call the hook** before `_stage_provider_policy` at `bin/mini-ork-self-improve:90`,
   inside an `if [[ -f "$MINI_ORK_DB" ]]; then _promote_resolved_learnings; fi`
   guard.
4. **No schema migration needed** — `learning_record.resolved_at` is already
   nullable in the existing migrations.

**Regression test.** `tests/unit/test_promote_resolved_learnings.sh` must
contain three assertions (verbatim text from `lens-correctness.md:121-124`):
- `assert_equal "resolved" "$outcome" "deferred row must promote on green test"`
- `assert_equal "deferred" "$outcome" "deferred row must stay on failed test"`
- `assert_equal "deferred" "$outcome" "row without test path must be left alone"`

Fixture setup: insert three `learning_record` rows into a tmp sqlite db, one
pointing at `tests/unit/test_verifier_ref_json.sh` (real, passing), one at a
synthetic `tests/unit/test_always_fails.sh` (created in-test, exits 1), one
with `evidence_paths='[]'`. Invoke `_promote_resolved_learnings` against the
tmp db and assert each row's `outcome`.

**Verification.** Must continue to pass:
- `tests/unit/test_verifier_ref_json.sh` (the adapter test itself).
- `tests/integration/test_recursive_self_improve_recipe.sh` (recipe end-to-end,
  per `lens-correctness.md:147`).
- `recipes/recursive-self-improve/verifiers/bottlenecks-found.sh` (no new
  envelope leakage).

Expected benchmark deltas:
- `learning_record.resolved_count`: 0 → 1+ after first run (positive).
- `bin/mini-ork-self-improve` startup wall time: +~50-200ms per `deferred`
  row that has a test path. Acceptable; gated by env var.
- No effect on `execution_traces` or `benchmark_results` row counts.

**Rollback criteria.** Discard the patch if any of the following hold after
landing:
- The hook flips any `learning_record` row whose test script does not
  contain an `assert_*` call (mitigated by the `grep -q assert_` guard;
  still verify in CI).
- `bin/mini-ork-self-improve` startup latency exceeds 2 s at the 95th
  percentile across three consecutive runs.
- `tests/unit/test_promote_resolved_learnings.sh` becomes flaky (>1 false
  promotion per 10 runs) — disable via `MINI_ORK_PROMOTE_LEARNINGS=0` and
  drop to lower-ranked queue for re-design.
- The `UPDATE … WHERE id=1` backfill collides with a future migration that
  reassigns `learning_record` ids — pre-empt by gating the targeted backfill
  on `… AND title LIKE 'Verifier verdict JSON adapter%'`.

## Lower-ranked patches

### Patch 2: Rename `traces` → `execution_traces` in bottleneck-scan prompt

- **Problem.** `recipes/recursive-self-improve/prompts/bottleneck-scan.md:14`
  documents `Key tables: traces` but the live schema has only
  `execution_traces` (`db/migrations/0010_benchmarks.sql:12`). Scanners that
  trust the prompt produce under-informed bottleneck lists.
- **Change.** Edit the single line to read
  `Key tables: execution_traces, benchmark_results, pattern_records, learning_record`.
  Audit remainder for stray `traces` (none expected per `lens-correctness.md:131`).
- **Regression test.** Add to recipe integration test:
  `assert_grep "execution_traces" recipes/recursive-self-improve/prompts/bottleneck-scan.md` and a
  negative match `! grep -wE "^[[:space:]]*Key tables:.*[^_]traces($|[^_])" …`.
- **Verification.** No code paths read this prompt programmatically; only
  LLM consumption. No bench delta expected.
- **Rollback.** Revert only if a future migration renames the table back.

### Patch 3: Populate `duration_ms` in every rich-trace write

- **Problem.** `_trace_write_node_rich` builds the trace JSON with seven keys
  (`cost_usd`, `tool_calls`, `files_read`, `files_written`, `final_artifact_ref`,
  `reviewer_verdict`, `verifier_output`) but never `duration_ms`. 16 of 18
  cost-bearing rows in `execution_traces` have `duration_ms=0`, making p95
  unusable and `task_class.yaml:38`'s `max_minutes: 60` cap unenforceable.
- **Change.** Per `lens-perf.md:32` (F-1):
  - At `bin/mini-ork-execute:441`, replace the trace-id line with:
    `local _node_t0_ms=$(python3 -c "import time;print(int(time.time()*1000))")`
    immediately before `NODE_TRACE_ID="tr-${node_type}-…"`.
  - In each caller (researcher `:496`, implementer `:517`, reviewer `:583`),
    compute `local _node_t1_ms=$(python3 -c "…"); local _duration_ms=$((_node_t1_ms - _node_t0_ms))`
    and pass `_duration_ms` as a 7th positional arg.
  - In the python3 builder at `:315-354`, accept the new positional and emit
    `obj['duration_ms'] = int(duration_ms)`.
- **Regression test.** Add benchmark task `id="trace-duration-populated"`
  to `lib/benchmark_suite.sh`; assert
  `SELECT COUNT(*) FROM execution_traces WHERE duration_ms > 0` is >0
  after a single recipe run. Add to `no-regression.sh`:
  `duration_ms_coverage = COUNT(duration_ms>0)/COUNT(*) >= 0.9`.
- **Verification.** Existing `test_e2e_benchmark_run.sh` must still pass.
  Expected: zero-duration row fraction drops from ~88% to <10% within one
  cycle; no wall-time regression.
- **Rollback.** Discard if `date`/`python3` shim is inconsistent across
  branches and `duration_ms` ever exceeds the cycle wall time, or if any
  builder-side `obj.get('duration_ms', 0)` reader breaks downstream.

### Patch 4: Expand pollution scan to every durable artifact

- **Problem.** `verifiers/bottlenecks-found.sh:58` only scans six files for
  `★ Insight` / `<z-insight>` leaks; `artifact_contract.yaml:11` declares
  `arxiv-refs.md` and patch outputs as durable too. The previous iter caught
  this for `lens-*.md` files (commit `e95e641`) but did not extend to the
  fallback name `arxiv-refs.md`.
- **Change.** Replace the hard-coded list with an explicit array
  `(lens-bottleneck.md lens-perf.md lens-correctness.md lens-arch.md lens-arxiv.md synthesis.md arxiv-refs.md)`;
  anchor both regex branches at `^` so prose mentions don't false-positive;
  push polluted paths into the existing `missing[]` JSON shape.
- **Regression test.** Per `lens-correctness.md:147`, add a fixture
  `lens-arch.md` with `<z-insight>` at line start to
  `tests/integration/test_recursive_self_improve_recipe.sh`; assert
  `pass=false` and `"lens-arch.md" ∈ missing[*]`.
- **Verification.** Existing single-file synthesis check must remain a subset
  of the new loop. No bench delta expected.
- **Rollback.** Revert if a legitimate lens intentionally quotes the envelope
  inside a fenced code block at line start — then narrow regex to
  start-of-file only.

### Patch 5: Cache cost-circuit "spent_today" + drop second python3 fork

- **Problem.** `lib/llm-dispatch.sh:348-368` forks `python3` twice per LLM
  call to (a) sum 24h `task_runs.cost_usd` and (b) compare to budget. Lane
  cache at `:373-414` was added for the same anti-pattern but the cost
  circuit was not retrofitted. ~70-110 ms wasted per dispatch.
- **Change.** Per `lens-perf.md:40` (F-2) + `lens-perf.md:48` (F-3):
  - Read `MO_COST_CIRCUIT_LAST` (epoch seconds) and `MO_COST_CIRCUIT_LAST_SPENT`.
    If `now - MO_COST_CIRCUIT_LAST < ${MO_COST_CIRCUIT_TTL:-15}`, reuse.
  - Otherwise run the python3 sum, then `export` the new pair.
  - Replace the second `python3 -c "import sys; sys.exit(…)"` with
    `awk -v s="$_spent_today" -v b="$_budget" 'BEGIN{exit !(s+0>=b+0)}'`.
  - Add a budget format guard: `[[ "$_budget" =~ ^[0-9.]+$ ]]`.
  - In `bin/mini-ork-execute:259-285`, after a successful `_d022_charge_node_cost`
    write, `unset MO_COST_CIRCUIT_LAST MO_COST_CIRCUIT_LAST_SPENT` so the
    next dispatch refreshes.
- **Regression test.** Benchmark task `id="cost-circuit-cache-warm"`: 5
  sequential `llm_dispatch` calls against a stub DB; assert total wall time
  < 1500ms and assert circuit still trips with `MO_DAILY_BUDGET_USD=0.0001`.
- **Verification.** Existing dispatch tests must pass. Expected savings:
  ~560-880ms per 8-dispatch cycle; ×4 at `MINI_ORK_MAX_PARALLEL=4`.
- **Rollback.** Discard if any in-window cost overshoot exceeds $1 due to
  TTL staleness; drop default `MO_COST_CIRCUIT_TTL` to 5 first; if still
  unsafe, remove the cache and keep only the F-3 awk swap.

## Convergence assessment

**Not at diminishing returns.** Mini-ork has cycled the same top-3 correctness
findings (B1, B2, B3) across iter 1 → iter 4 because the implementer node
has not landed any of them. Per `lens-correctness.md:179` (Open Q 4), the
gap is structural: dedupe cannot mark resolved work as resolved (Patch 1) and
the verifier suite never fails on stale findings, so the outer loop has no
forcing function. Once Patch 1 lands and `_promote_resolved_learnings` is
wired in, the next iteration's bottleneck scanner will suppress the closed
items at the source and the loop can begin meaningful descent. Recommendation:
**continue the outer loop**, and re-evaluate convergence after iter 5 if
Patch 1 has landed; if Patch 1 still has not landed in iter 5, the outer
loop should escalate to a human via `requires_user_action` rather than
re-iterate.

Notable open questions deferred to the implementer / user:
- `lens-perf.md:56` Q1: should the implementer write a `learning_record`
  insert alongside Patch 5, or defer to the reflector? **Synthesizer
  decision:** the reflector owns post-merge `learning_record` writes;
  implementer only writes for Patch 1's targeted backfill (id=1) because
  that *is* the patch.
- `lens-perf.md:58` Q3: enforce `max_minutes: 60` in `no-regression.sh` as
  part of Patch 3? **Synthesizer decision:** keep separate; Patch 3 is
  observability-only this iteration.
- `lens-perf.md:59` Q4: is adding a benchmark task an arch-level change?
  **Synthesizer decision:** allowed at the implementer scope for Patches 3
  and 5; benchmarks are recipe artifacts, not infra.
- `lens-correctness.md:175` Q2: implicit vs. explicit baseline in B4
  benchmark gate. **Synthesizer decision:** B4 is queued (not in top 5);
  resolve when promoted.
- `lens-arch.md:114-116` Qs: artifact metadata + policy ref. **Synthesizer
  decision:** queue as iter-5 candidates; both architecture patches
  explicitly say "no new infra" and need no arXiv evidence.

## Provenance footer

- Lenses consumed: minimax (perf) / kimi (correctness) / codex (arch) + bottleneck scan
- Lenses absent: arxiv lens (`lens-arxiv.md` not present in run dir or
  worktree); not load-bearing for this synthesis because no ranked patch
  proposes new infra (graph DB, table, wrapper, or MCP tool), so the
  "new infra requires arXiv evidence" rule does not trigger
- Synthesizer family: opus
- arXiv papers cited: 0
- Cross-iteration learnings applied: `learning_record` queried in
  `lens-bottleneck.md:21-22` — `resolved_count=0`, `id=1` deferred,
  `pattern_records` empty; iter-1/iter-2/iter-3 syntheses at
  `docs/improvements/self-improve-latest.md` used for dedupe of B1/B2/B3
  and perf row #6
- Provider policy: per `MINI_ORK_PROVIDER_POLICY` →
  `/Volumes/docker-ssd/ps/mini-ork/.mini-ork/config/agents.yaml`;
  researcher lanes routed through `minimax_lens` / `kimi_lens` / `codex_lens`
  (non-Anthropic families); only `opus_synthesizer` routed to Anthropic
