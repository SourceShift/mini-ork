# Synthesis — Recursive Self-Improvement, iter 34

Run ID: `self-improve-iter-34-20260609115529`
Worktree: `/Users/admin/ps/mini-ork/.mini-ork/worktrees/iter-34-20260609115529`
HEAD at start: `8f11814`

## Ranked patch plan

| Rank | Bottleneck | Category | Patch summary | Evidence | Confidence |
|---|---|---|---|---|---|
| 1 | iter-34 verifier `v8_provider_policy_respected` queries a non-existent table `llm_dispatch`; the gate is silently vacuous AND `llm_calls` has no producer | correctness | Wire `_mo_llm_write_llm_calls_row` in `lib/llm-dispatch.sh` to persist provider/model/tier/cost/duration/actor per call, then rewrite the planner template's `v8` check to query `llm_calls` (`actor`, `ts`) instead of `llm_dispatch` (`role`, `created_at`) | bottleneck #1 (`plan.json:179`), correctness #1, arch Cand 1, perf F2; `db/migrations/0002_mini_orch_sessions.sql:220-242`; `lib/llm-dispatch.sh:48-51,348-468`; arXiv 2604.17092 (Bhati, 0.82), 2604.21083 (Lin, 0.76) | 0.88 |
| 2 | 87/87 `recursive_self_improve` rows today have `duration_ms=0` and `cost_usd=0` — plan/classify wrappers build inline JSON instead of reading the dispatch sidecars | perf | Add `trace_write_node` helper to `lib/trace_store.sh` that reads `${MINI_ORK_RUN_DIR}/.last-llm-{cost,duration-ms}` with a freshness window, then swap the 11 inline `trace_write "{...}"` call-sites in `bin/mini-ork-plan` (9) and `bin/mini-ork-classify` (2) | bottleneck #3, perf F1; `bin/mini-ork-plan:108,302,316,492,500,507,515,522,572`; `bin/mini-ork-classify:113,304`; `bin/mini-ork-execute:301-365` (existing rich writer to share); no new arXiv required (refactor of existing infra) | 0.84 |
| 3 | `trace_write … 2>/dev/null \|\| true` is the project-wide idiom; the D-039 postmortem at `lib/trace_store.sh:35-50` records a 10+-DF-cycle silent outage caused by exactly this pattern | correctness | Add `trace_write_or_log` wrapper in `lib/trace_store.sh` that routes stderr to `${MINI_ORK_RUN_DIR}/trace-write-errors.log` while preserving caller exit code; mechanically sweep 26 call sites in `bin/mini-ork-{plan,classify,execute,verify,promote}` and `lib/circuit_breaker.sh:441,465` | bottleneck #5, correctness #5, arch Cand 2, perf F3; `lib/trace_store.sh:35-50` postmortem; no directly relevant arXiv (lens marked `no-relevant-papers-found`) but repo-local evidence is sufficient (idiom refactor only, no new infra) | 0.80 |
| 4 | Synthesis→`learning_record` promotion gap: iters 21–31 produced ranked synthesis but no DB rows; iter-34's dedup scan had to text-scrape prior markdown | arch | Add `_promote_synthesis_findings` to `bin/mini-ork-self-improve` that parses the synthesis ranked table into `learning_record` candidates keyed by `(run_id, iter, rank, title)`; idempotent; called from `_self_improve_record_success` | bottleneck #7, arch Cand 3; `bin/mini-ork-self-improve:178-217,481-484`; `db/migrations/0017_self_improve_learning.sql:30-52`; arXiv 2605.05724 (Ning, 0.84), 2503.20576 (Guo, 0.70) | 0.74 |
| 5 | `pattern_records` table has 0 rows; promotion pipeline starves because no path mines `execution_traces` clusters into patterns | arch | Add `pattern_miner` step that groups `execution_traces` by `(task_class, reviewer_verdict)` and upserts via `lib/pattern_store.sh` when cluster size ≥ N over a rolling window; gated behind `MO_PATTERN_MINER=1` for safety | bottleneck #6, arch Cand 3; `lib/pattern_store.sh:52-145`; `db/migrations/0011_evolution.sql:45-52`; arXiv 2603.10600 (Fang, 0.86), 2604.10513 (Ben-Gigi, 0.80) | 0.70 |

## Top patch — detailed plan

### Patch 1: Wire `llm_calls` producer + align iter-34 `v8` verifier query

**Problem statement.** Iter-34's own verifier `v8_provider_policy_respected` issues `SELECT COUNT(*) FROM llm_dispatch WHERE role='researcher' AND provider='anthropic' AND created_at > datetime('now','-6 hours')` against a table that does not exist (`db/migrations/0002_mini_orch_sessions.sql:220-242` defines only `llm_calls` with columns `actor`/`ts`). The shell pipe `| grep -q '^0$'` receives the SQLite error on stderr and an empty stdout, so the gate silently mis-fires (the lens disagrees on whether it false-passes or false-fails depending on SQLite version; either way the policy invariant is not actually checked). Even if the table name is corrected, `llm_calls` has 0 producers — the gate is vacuous until a writer ships.

**Evidence.**
- `plan.json` line 179: `"command": "sqlite3 ... \"SELECT COUNT(*) FROM llm_dispatch WHERE role='researcher' AND provider='anthropic' AND created_at > datetime('now','-6 hours');\" | grep -q '^0$'"`.
- Schema truth: `db/migrations/0002_mini_orch_sessions.sql:220-242` (columns: `actor`, `ts`, `provider`, `model_id`, `tier`, `feature_name`, `input_tokens`, `output_tokens`, `cost_usd`, `duration_ms`, `status`, `traceparent`, `metadata_json`, `iter`).
- `sqlite3 state.db "SELECT COUNT(*) FROM llm_calls;"` → `0`.
- `grep -rn "INSERT INTO llm_calls\|llm_calls(" --include="*.go" --include="*.sh"` → `0 hits`.
- Producer-shim site: `lib/llm-dispatch.sh:48-51` (`_mo_llm_write_duration_ms` writes only the sidecar file); `lib/llm-dispatch.sh:453-468` (success path computes `_duration_ms` and copies cost sidecar but never inserts a DB row).
- arXiv evidence (mandatory because new helper function is new infra): `lens-arxiv.md` cites `2604.17092` "AI Observability for Developer Productivity Tools" (Bhati 2026, conf 0.82) for the provider/model/tier ledger pattern; `2604.21083` "Behavioral Consistency and Transparency Analysis on Large Language Model API Gateways" (Lin 2026, conf 0.76) for the per-call audit row shape.

**Proposed change.**

1. In `lib/llm-dispatch.sh`, add a new helper after `_mo_llm_write_duration_ms` (around line 60):

   ```sh
   # _mo_llm_write_llm_calls_row: persist a per-call audit row in llm_calls.
   # Best-effort: guarded by MINI_ORK_DB writability + busy_timeout.
   # Args: provider model_id tier feature_name actor status duration_ms cost_usd error_message
   _mo_llm_write_llm_calls_row() {
     local provider="$1" model_id="$2" tier="$3" feature_name="$4"
     local actor="$5" status="$6" duration_ms="$7" cost_usd="$8" error_message="$9"
     [ -n "${MINI_ORK_DB:-}" ] && [ -f "$MINI_ORK_DB" ] || return 0
     local iter="${MO_RECURSIVE_ITER:-}"
     local run_id="${MINI_ORK_RUN_ID:-}"
     local traceparent="${MO_TRACEPARENT:-}"
     python3 - "$MINI_ORK_DB" "$provider" "$model_id" "$tier" "$feature_name" \
                              "$actor" "$status" "$duration_ms" "$cost_usd" \
                              "$error_message" "$iter" "$run_id" "$traceparent" <<'PY' 2>>"${MINI_ORK_RUN_DIR:-/tmp}/trace-write-errors.log" || true
   import sqlite3, sys
   db, *args = sys.argv[1:]
   con = sqlite3.connect(db, timeout=5)
   con.execute("PRAGMA busy_timeout=5000")
   con.execute(
       "INSERT INTO llm_calls (provider, model_id, tier, feature_name, actor, "
       "status, duration_ms, cost_usd, error_message, iter, run_id, traceparent) "
       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
       (args[0], args[1], args[2], args[3], args[4], args[5],
        int(args[6] or 0), float(args[7] or 0.0), args[8] or None,
        int(args[9]) if args[9] else None, args[10] or None, args[11] or None),
   )
   con.commit()
   con.close()
   PY
   }

   # Derive provider from model name (consistent with _MO_LLM_EXECUTABLE_MODELS).
   _mo_llm_provider_for_model() {
     case "$1" in
       codex|gpt-*|o1*|o3*) printf 'openai\n' ;;
       gemini*|*-gemini-*) printf 'google\n' ;;
       minimax*|glm*|kimi*|deepseek*) printf 'gateway\n' ;;
       *) printf 'anthropic\n' ;;
     esac
   }
   ```

2. In `lib/llm-dispatch.sh` `llm_dispatch` success branch (around line 460, after `_mo_llm_write_duration_ms`), call:

   ```sh
   _mo_llm_write_llm_calls_row \
     "$(_mo_llm_provider_for_model "$model")" "$model" "${MO_LANE_TIER:-default}" \
     "mini-ork:${MO_NODE_TYPE:-unknown}" "${MO_LANE_ACTOR:-${USER:-unknown}}" \
     "success" "$_duration_ms" "$(cat "${out_file}.cost" 2>/dev/null || printf 0)" ""
   ```

   On the failure branch (rc≠0), call the same helper with `status="failed"`, `cost_usd=0`, and `error_message="$(tail -c 200 "$err_file" 2>/dev/null || true)"`.

3. Patch the planner template that emits `v8`. The query lives in the planner LLM's structured output, written into `plan.json` by `bin/mini-ork-plan`. Update the planner system prompt (`recipes/recursive-self-improve/prompts/planner.md` or equivalent) to specify the canonical query:

   ```
   sqlite3 ${MINI_ORK_DB} "SELECT COUNT(*) FROM llm_calls
     WHERE actor='researcher' AND provider='anthropic'
     AND ts > datetime('now','-6 hours');" | grep -q '^0$'
   ```

   For iter-34 only (one-shot), also patch `plan.json:179` in the run directory so the local verifier becomes meaningful immediately.

**Regression test.** Add `tests/unit/test_llm_calls_ledger.sh`:

```sh
#!/bin/bash
# Asserts that a successful dispatch writes a row to llm_calls and that the
# row carries non-empty provider, model_id, status='success', duration_ms>=0.
set -euo pipefail
export MINI_ORK_DB="$(mktemp -t mini-ork-XXXXXX.db)"
sqlite3 "$MINI_ORK_DB" < db/migrations/0002_mini_orch_sessions.sql
export MINI_ORK_RUN_DIR="$(mktemp -d)"
source lib/llm-dispatch.sh
# Simulate the post-dispatch write.
_mo_llm_write_llm_calls_row "anthropic" "sonnet" "default" \
  "mini-ork:test" "tester" "success" "1234" "0.0021" ""
COUNT=$(sqlite3 "$MINI_ORK_DB" "SELECT COUNT(*) FROM llm_calls WHERE status='success' AND duration_ms=1234;")
[ "$COUNT" = "1" ] || { echo "FAIL: expected 1 row, got $COUNT"; exit 1; }
echo "PASS: llm_calls ledger writes success row"
```

Assertion text: `expected exactly 1 row in llm_calls with status='success' AND duration_ms=1234 after successful dispatch`.

**Verification.**
- `go test ./...` continues to pass (no Go code touched).
- `tests/unit/test_llm_calls_ledger.sh` PASS.
- New gate works end-to-end: run iter-34's `v8` after this patch lands — should return `exit=0` with `0` matching rows because `actor='researcher'` writes go to non-anthropic lanes per `agents.yaml` policy.
- Expected delta: `+24K+ rows/day` on active iter days (per F2 estimate); `llm_calls.cost_usd` rollup becomes computable, unblocking per-provider spend dashboards. Sign: monotone-increasing row count; magnitude: ~100 rows per recursive-self-improve iter.

**Rollback criteria.**
- Any `tests/unit/test_*.sh` that touched `lib/llm-dispatch.sh` regresses.
- `SQLITE_BUSY` errors observed in `trace-write-errors.log` exceed 1% of successful dispatches over 1h (writer hot-path contention).
- Provider derivation misclassifies a gateway model as anthropic, which would cause `v8` to false-flag a legitimate non-anthropic researcher dispatch. Detect by sampling `llm_calls.provider` distribution against `agents.yaml` lane truth after first 100 rows; if mismatch >5%, revert and re-derive from `agents.yaml.lanes.<node>.provider` directly.

## Lower-ranked patches

### Patch 2: `trace_write_node` helper + plan/classify wrapper swap (perf)

**Problem.** Plan/classify `trace_write` payloads omit `duration_ms` and `cost_usd`; 87/87 `recursive_self_improve` rows today carry `duration_ms=0`.
**Change.** Hoist `_trace_write_node_rich` from `bin/mini-ork-execute:301-365` into `lib/trace_store.sh:trace_write_node`; replace 11 inline call-sites in `bin/mini-ork-plan` and `bin/mini-ork-classify`; add freshness-window guard (`5 * MO_DISPATCH_TIMEOUT`, default ~7500s).
**Test.** Benchmark `rt_richness_001` asserts `duration_ms > 1000 AND cost_usd > 0` after a planner dispatch.
**Rollback.** Stale sidecar reads → wrapper picks up prior-run values; mitigated by freshness window. Revert if `trace_write_node` is called from a subshell without `MINI_ORK_RUN_DIR`.

### Patch 3: `trace_write_or_log` wrapper (correctness)

**Problem.** `trace_write … 2>/dev/null || true` masks schema drift (D-039 postmortem).
**Change.** Add `trace_write_or_log` to `lib/trace_store.sh`; sweep 26 call sites mechanically; route stderr to `${MINI_ORK_RUN_DIR}/trace-write-errors.log` with rotation knob `MO_TRACE_ERR_LOG_MAX_BYTES=1048576`.
**Test.** `trace_drift_001` benchmark forces an invalid column, asserts log line appears + caller exit code stays 0.
**Rollback.** Transient `SQLITE_BUSY` spikes flood the log → tighten rotation, do not revert silencing.

### Patch 4: Synthesis→`learning_record` promoter (arch)

**Problem.** Iters 21–31 ranked synthesis but never inserted rows; dedup must text-scrape.
**Change.** Add `_promote_synthesis_findings` to `bin/mini-ork-self-improve` after `_self_improve_record_success` (line 484). Parse the synthesis ranked table by regex (`| Rank | Bottleneck | ... |`), insert with idempotency key `(run_id, iter, rank, title)`.
**Test.** Parse iter-32 synthesis fixture, assert 5 rows materialize in `learning_record` with stable keys.
**Rollback.** Markdown shape drift breaks parser → gate behind `MO_PROMOTE_SYNTHESIS=1`; only enable on confirmed synthesizer template.

### Patch 5: `pattern_miner` over `execution_traces` (arch)

**Problem.** `pattern_records` = 0 rows; promotion pipeline starves.
**Change.** Add `bin/mini-ork-pattern-miner` that groups `execution_traces` by `(task_class, reviewer_verdict)` over a rolling 7-day window; upsert via `lib/pattern_store.sh` when cluster size ≥ 5; output_type derived from verdict taxonomy.
**Test.** Seed 5 failure rows with `reviewer_verdict='llm_dispatch_failed'`, run miner, assert one `pattern_records` row with `output_type='verifier_addition'`.
**Rollback.** Noisy clusters promote spam → gate behind `MO_PATTERN_MINER=1`, require operator review for `prompt_change` output_type.

## Convergence assessment

**Not yet at diminishing returns.** Iter-34 surfaced a self-referential bug (the iter's own verifier is broken) that prior iters did not catch, plus two empty-schema clusters (`llm_calls`, `pattern_records`) that have been visible-but-unranked since iter-32. Iter-33's #2 (plan/classify `duration_ms=0`) re-ranked here as Patch 2 because no feat commit landed between `b9b6d18` and `8f11814`. The outer loop should continue: iter-35 will likely surface the synthesis→`learning_record` promotion gap (Patch 4) once iter-34's Patch 1 lands and dedup can shift from markdown-scrape to structured queries.

Signals that suggest convergence is approaching but not here yet:
- 4 of 5 ranked patches now target the same "empty schema, no producer" anti-pattern (`llm_calls`, `pattern_records`, `learning_record` 21-31 gap, trace-write idiom).
- arXiv refs are converging on the trajectory-memory / closed-loop self-improvement cluster (2603.10600, 2604.10513, 2605.05724) — same paradigm cited 3 times.
- Cross-family lens agreement is high (3/3 lenses ranked the `v8` bug; 3/3 cited the trace-write idiom).

Continue iterating for ≥2 more cycles; reassess at iter-36.

## Provenance footer

- Lenses consumed: minimax (perf), kimi (correctness), codex (arch), arxiv research lane
- Synthesizer family: opus (Anthropic)
- arXiv papers cited: 8 (2604.17092, 2604.21083, 2509.25370, 2602.02475, 2603.10600, 2604.10513, 2605.05724, 2503.20576)
- Cross-iteration learnings applied: 15 rows scanned from `learning_record` (iter ∈ {0,1,18,19,20,32,33}); deduped against iter-33 ranked synthesis carry-forwards (#2 → Patch 2, #5 → Patch 4)
- Dedup table: bottleneck #1 (verifier `llm_dispatch` drift) is novel to iter-34 — no prior `learning_record` row matches this fingerprint; sibling iter-33 `learning_record.fingerprint` drift was a different column on a different table
