# Scalability Audit — Synthesis (run-1780298691-99474)

> 4-lens audit of mini-ork v0.1.1 → v0.2-pt13. Lenses: **GLM** (tactical
> bottlenecks), **Kimi** (code refactor diffs), **Codex** (LLM dispatch
> cost), **Opus** (architectural shape).
> Read-only. HEAD = `6d70157`. 2026-06-01.

ID conventions used in this doc:
- `G-N` → GLM finding row N (see `lens-glm.md`)
- `K-N` → Kimi refactor N (see `lens-kimi.md`)
- `D-N` → Codex finding N (see `lens-codex.md`)
- `O-RN` → Opus recommendation N (see `lens-opus.md §7`)
- `★` = 2-lens consensus (e.g. GLM + Kimi) · `★★` = 3-lens (e.g. GLM + Kimi + Codex) · `★★★` = all-4-lens (GLM + Kimi + Codex + Opus)

---

## Section 1 — Severity × Leverage Matrix

Rows: blast radius (P1 = bleeds today, P2 = breaks at 100K/day, P3 = breaks at 10M/day).
Cols: leverage (HIGH = changes the slope, MED = changes the constant, LOW = correctness/hygiene).

|        | **HIGH leverage** | **MED leverage** | **LOW leverage** |
|--------|------------------|------------------|-------------------|
| **P1 — bleeds today** _(P1 sources: GLM/Kimi/Codex/Opus)_ | **K-1 ★★ G-1 D-2** serial gradient LLM loop (Kimi+GLM+Codex) · **D-7** double cost-charge D-009 / D-022 (Codex) · **D-1** cl_opus.sh pins all model slots (Codex) · **D-5** reviewer→opus default (Codex) | **K-9 ★ G-3** N+1 gradient lookup in failure linking (Kimi+GLM) · **K-6** per-line git blame (Kimi) · **G-5 K-9-risk** missing `gradient_records.evidence` index (GLM+Kimi) | **K-3 G-4 D-4** ★ python3 float-validation fork (Kimi+GLM+Codex) · **K-5** python3 date-arith fork (Kimi) · **K-7 G-8 G-9 ★** raw sqlite3 bypasses `mo_sqlite` (Kimi+GLM) |
| **P2 — 100K/day** _(P2 sources: GLM/Kimi/Codex/Opus)_ | **K-11 G-11 ★** lane-cache subshell scope (Kimi+GLM) · **D-8** xhigh effort on mechanical nodes (Codex) · **D-15** no `--max-turns` cap (Codex) · **O-R3** persistent SQLite daemon (Opus) | **G-2 G-7** N+1 sqlite3 forks in auto-merge (GLM) · **K-13 G-10** O(N²) jq subshell (Kimi+GLM) · **K-12 G-29 O-R3 ★** open/commit/close per write (Kimi+GLM+Opus) · **G-12** batch-flush vs sliding window (GLM) | **G-24 D-13 ★** TEXT/INTEGER affinity in `task_runs.created_at` (GLM+Codex) · **G-17 O-R9 ★** `mo_events` no archival trigger (GLM+Opus) · **K-7 K-10 ★** string-interpolated SQL injection class (Kimi) · **D-9** executable wrappers skip cache flags (Codex) |
| **P3 — 10M/day & long-horizon** | **O-R1** state.db → PostgreSQL · **O-R4** queue-backed LLM dispatch · **D-A** task_class × node_type × tokens router · **D-C** Anthropic Batches API for reflection | **O-R2** monthly range partitioning + TTL · **O-R6** OTLP exporter on existing `traceparent` · **O-R7** per-recipe cost circuit breaker · **D-B** semantic gradient cache (embeddings) | **O-R8** kill inline `_ensure_table` DDL guards · **O-R10** recipe signing for registry · **G-25** documented `--no-verify` gate · **G-23** `MO_TRACE_QUERY_LIMIT=0` silent-loss guard |

**Consensus density.** 11 distinct findings hit ≥2 lenses; 3 hit ≥3 lenses (★★ or higher). The 4-lens overlap (GLM + Kimi + Codex + Opus) on **python3-fork overhead on the LLM/DB hot path** (K-3, K-5, K-8, K-12, G-4, G-10, G-22, G-29, D-4, D-14, O-R3) is the single highest-leverage substrate-level pattern in the audit.

---

## Section 2 — Top 5 Immediate Wins (P1, total effort ≤ 2 weeks)

These ship as ordinary patches. No schema migration. No new infra. ROI computed at the documented 100K/day target.

### W1 — Batch gradient extraction (K-1 ★★ + G-1 + D-2; sources Kimi + GLM + Codex) — **3 days**
**Site**: `lib/reflection_pipeline.sh:65-73`
**Fix**: replace the `while read tid` serial loop dispatching one `gradient_extract` per trace with a batched LLM call (20 traces per request, model emits an array-of-arrays keyed by `trace_id`). Combine with K-1's bulk `executemany` for `gradient_records`.
**ROI**: 500 → ~25 LLM round-trips per reflect cycle. At sonnet $0.003/call: $1.50 → $0.08 per cycle (95% cut). Wall time 16 min → ~1 min.
**Caveat**: K-1 risk note — bulk path skips `_PATTERN_ON_NEW_HOOKS`. Audit hook consumers before flipping; if any consumer relies on hook fan-out, fire the hooks once per batch at the end.

### W2 — Kill the D-009 double cost-charge (D-7) — **half day**
**Site**: `bin/mini-ork-execute:718-737` overlapping `bin/mini-ork-execute:376,396,447`
**Fix**: delete the D-009 flat `$0.01 × DISPATCHED_COUNT` charge now that D-022 + D-029 record real `total_cost_usd` per node. Or gate D-009 behind `[ "$MO_D029_REAL_COST" != 1 ]`.
**ROI**: per-run cost ledger overstated by $0.06/run. At 100K runs/day = $6K/day in phantom spend triggering the circuit breaker ~2× too early. Fixing this *doubles* effective daily budget headroom without raising the cap.
**Caveat**: must be paired with confirmation that D-029 fires on *every* node (verifier nodes that don't dispatch LLM still need to record `$0.00` to avoid leaving cost_usd NULL — currently the D-009 block was hiding this).

### W3 — Fix the opus-fan-out in `cl_opus.sh` (D-1) — **15 minutes**
**Site**: `lib/providers/cl_opus.sh:11-14`
**Fix**: keep `ANTHROPIC_MODEL=claude-opus-4-7` and `ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-7`. Set:
```sh
export ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-6
export ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4-5-20251001
export CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-4-6  # not opus
```
**ROI**: every internal sub-agent dispatch (TodoWrite, Agent, file-read tool calls inside an opus session) currently runs on opus-4-7. Sonnet is 25× cheaper, haiku ~90× cheaper. Conservative estimate at 100K/day reviewer frequency: ~$8K/day saved.
**Caveat**: validate the codex lens's own claim against a single `lens-opus` re-run with the patched env to confirm Claude Code actually honors the new sub-agent var. The original choice may have been a quality safeguard — keep an env flag (`MO_OPUS_PIN_ALL=1`) for the rare case a brain session genuinely needs opus-only sub-agents.

### W4 — Cast `created_at` consistently (G-24 ★ D-13; sources GLM + Codex) — **1 day**
**Site**: `lib/llm-dispatch.sh:207` (cost circuit breaker) + `db/migrations/0013_task_runs.sql:37` (column type)
**Fix**: the circuit-breaker query compares INTEGER `cutoff` to TEXT `created_at` columns — SQLite type-affinity mismatch silently corrupts the comparison. Either:
- (a) update query to `WHERE CAST(strftime('%s', created_at) AS INTEGER) >= ?` (matches the pattern already used at `lib/reflection_pipeline.sh:53-55`), or
- (b) write a new migration that standardises every `created_at` column to INTEGER epoch — preferred, removes the entire class of bug.
**ROI**: wrong daily-spend totals → circuit-breaker fires at the wrong threshold (sometimes too early, sometimes never). Also restores index usage on `task_runs.created_at` for those time-windowed queries (full-scan → O(log N)).
**Caveat**: option (b) is a bigger lift because `execution_traces.created_at` is also TEXT (G-24); back-fill conversion has to run against existing rows.

### W5 — Fix lane-cache subshell scope (K-11 ★ G-11; sources Kimi + GLM) — **half day**
**Site**: `lib/llm-dispatch.sh:228-236`
**Fix**: replace `declare -gA _MO_LANE_CACHE` (which doesn't survive `( … ) &` subshells used by parallel node dispatch) with an exported env var per node-type:
```sh
local _safe_key="_MO_LANE_${node_type^^}"
_safe_key="${_safe_key//-/_}"
local _cached_model="${!_safe_key:-}"
if [ -z "$_cached_model" ]; then
  _resolved=$(python3 - "$_agents_yaml" "$node_type" <<'PY' …)
  export "$_safe_key=$_resolved"
fi
```
**ROI**: in parallel-dispatch mode (`MINI_ORK_MAX_PARALLEL=4`), the yaml parse currently fires N× per node-type instead of once. Eliminates 4 redundant `python3 yaml.safe_load` forks per parallel batch.
**Caveat**: K-11's hyphen-sanitisation step is mandatory — `node-type` style names blow up bash variable assignment otherwise.

**Total effort**: ~5–6 eng-days. **Total recovered**: ~$14K/day at 100K-task tier *plus* correctness fixes for two latent classes of bug (circuit-breaker affinity, parallel-mode cache miss).

---

## Section 3 — v0.x+1 Architectural Shifts (P2, bundle by theme)

### Bundle A — **Data-layer**: kill the python3-per-DB-op tax (1–2 eng-weeks)

- **A1**: persistent SQLite helper daemon (`bin/mini-ork-dbd`, Unix socket) — **O-R3 + G-22 + K-12 ★** (sources Opus + GLM + Kimi). 100-line Python daemon owns one long-lived `sqlite3.Connection`. All `lib/*.sh` python3 heredocs talk to it via socket instead of forking python3. Removes ~30–50 ms/op startup cost. At 100K/day = ~50% pipeline latency recovery.
- **A2**: route `lib/cache.sh` and `lib/runs-tracker.sh` through `mo_sqlite` — **G-8 + G-9 + K-7 ★** (sources GLM + Kimi). They currently bypass the busy-timeout wrapper; under concurrent WAL writes they silently return empty results.
- **A3**: add the two missing indices in the same migration: `idx_gr_evidence` and `idx_fl_trace` — **G-5 + G-6** (GLM). Prerequisite for K-9's (Kimi) JOIN rewrite to be O(log N).
- **A4**: standardise `created_at` columns to INTEGER epoch across `task_runs`, `execution_traces`, `mo_events`, `llm_calls` — **G-24 + D-13 ★** (sources GLM + Codex). Carries W4 forward at schema level.
- **A5**: archival trigger on `mo_events` — **G-17 + O-R9 ★** (sources GLM + Opus). Move rows older than N days to `mo_events_archive` (sibling table already exists since `0002_mini_orch_sessions.sql:127`).

**Total**: ~2 eng-weeks. **Prereq P1s**: W4, W5. **Risk if deferred**: A1 absent → 100K/day pipeline lives at ~40% of its theoretical throughput; A5 absent → state.db grows unboundedly and a single overnight surge fills the disk.

### Bundle B — **LLM dispatch**: graduate from synchronous subshells (2–3 eng-weeks)

- **B1**: Anthropic Messages Batches API for reflection — **D-C + K-1**. Reflection runs off the hot path; perfect candidate for the batch endpoint's 50% pricing. 500-trace cycle: $1.50 → $0.75, async.
- **B2**: task-class × node-type × prompt-token router — **D-A + D-10**. Two-dim routing table replaces flat `agents.yaml.lanes[node_type]`. Haiku for <2K-token verifier/publisher/rollback, sonnet middle, opus for >20K-token or reviewer/spec_reviewer/brain. Expected ~$1.3K/day saved on haiku-tier rerouting alone at 100K/day.
- **B3**: per-node `--max-turns` cap and `CLAUDE_CODE_EFFORT_LEVEL` map — **D-8 + D-15**. Researchers run 60 turns by default; most need ≤15. Verifiers at `xhigh` effort waste thinking-token budget. Hot fix wired off `node_type`.
- **B4**: provider fallback routing — **D-6**. The `fallback_above`/`fallback_below` fields in `config/agents/{kimi,glm,deepseek}.yaml` are currently dead metadata. Read them on `429|rate.limit|unavailable` errors and re-dispatch once to the fallback lane.

**Total**: ~3 eng-weeks. **Prereq P1s**: W3 (avoids B2 fighting `cl_opus.sh`'s blanket pinning). **Risk if deferred**: B1+B2 absent → reflection cycles stay at $30/cycle and the framework can't afford to reflect more than once/day; B4 absent → a kimi quota event = hard batch failure with no automatic recovery.

### Bundle C — **Runtime parallelism**: real sliding-window dispatch (3–5 days)

- **C1**: replace `_flush_parallel_batch`'s synchronous `wait` on N pids with `wait -n` loop (bash 4.3+) — **G-12**. Today's batch-flush caps throughput at *batches* of 4, not a continuous sliding window of 4.
- **C2**: drop `mo_aggregate_cache_stats` O(N²) jq accumulator → collect once, `jq -s` once — **K-13 + G-10**.

**Total**: ~4 eng-days. **Prereq P1s**: none. **Risk if deferred**: dashboards (`cache-stats` recipe) get slower as logs accumulate, parallel recipes throttle artificially.

### Bundle D — **Observability** (1 eng-week)

- **D1**: OTLP exporter wired to the existing `mo_events.trace_id` + `llm_calls.traceparent` fields — **O-R6**. 150-line Python daemon shipping spans to local Jaeger. No schema change.
- **D2**: per-recipe / per-model cost rollup VIEW + circuit breaker — **O-R7 + D-A**. Today's `MO_DAILY_BUDGET_USD` is a global cap; one expensive recipe can starve others.
- **D3**: stage-cache CHECK constraint expansion — **D-12**. Add `'gradient-extract'` and `'reflection-run'` to the allowed `stage` enum in `lib/cache.sh:26-29`, then wire `mo_cache_emit` at the end of `gradient_extract()`. Earns ~100% LLM cost skip on redundant reflect cycles.

**Total**: ~1 eng-week. **Prereq P1s**: W4 (so the cost rollup VIEW uses INTEGER created_at). **Risk if deferred**: gradient extraction debuggability collapses at 100K/day; one runaway recipe burns the global budget for the rest.

---

## Section 4 — Long-horizon (P3 + advisory)

These are tracked, not load-bearing yet.

- **L1 — O-R1 PostgreSQL migration**. Real cost: 2–3 eng-weeks. **Don't pull forward.** SQLite + Bundle A (persistent dbd) sustains 100K/day comfortably. The trigger to start is sustained 50K+ tasks/day, not a calendar date. `db/migrations/` is already versioned `.sql`, so the port is mechanical except the PL/pgSQL trigger translation for `0012_safety.sql`.
- **L2 — O-R2 monthly range partitioning + TTL archival**. Pairs with L1. Once PG is the substrate, partition `task_runs`, `mo_events`, `execution_traces`, `llm_calls`, `iters` by `created_at` monthly via `pg_partman`. Detach + Parquet-archive cold partitions to S3 after 90 days. Not meaningful pre-PG.
- **L3 — O-R4 queue-backed worker pool**. Required *only* at 10M/day (~115 task_runs/sec). Until then, persistent dbd + sliding-window parallelism (Bundle A1 + C1) is enough. When the trigger fires: 150-worker Python pool, `dispatch.db` as job table, `SELECT … FOR UPDATE SKIP LOCKED` semantics. Do **not** try to build this in Bash — Opus's lens is right that signal handling makes it unmaintainable.
- **L4 — D-B semantic gradient cache (embeddings)**. Pre-dispatch cosine-similarity check on `(status, task_class, verifier_output)` embeddings against existing `gradient_records`. Catches the "same BDD failure, different trace_id" duplication class that exact-hash dedup (D-3) cannot. Free with a local MiniLM model. Earns 60–80% cache hit at steady state — but only worth the complexity once the gradient library matures past ~10K records.
- **L5 — O-R10 recipe signing**. The `audit_log` append-only trigger is the right provenance primitive; extend it to log every recipe load with `SHA-256(workflow.yaml)`. Mandatory before any public recipe registry opens, not before.
- **L6 — O-R8 kill inline `_ensure_table` DDL guards**. Policy: DDL lives only in `db/migrations/`. The drift between inline `CREATE TABLE IF NOT EXISTS` in `lib/trace_store.sh` and the canonical migration was the cause of the v0.2-pt11.5 `gradient_records` data-loss class of bug. Apply repo-wide once Bundle A migrations land.
- **L7 — G-23 `MO_TRACE_QUERY_LIMIT=0` silent-loss guard**, **G-25 `--no-verify` env gate**, **G-26/G-27 DB-level DDL idempotency check** — pure hygiene items. Roll into the next refactor sprint, low priority but trivially cheap to fix.

---

## Section 5 — Hardest Open Question (inherited from `lens-opus.md §7`)

**How does the self-evolution loop behave under adversarial gradient injection at scale?**

The framework's central claim — documented in `docs/SAFETY.md §7-rung ladder` and `docs/ARCHITECTURE.md §evolution` — is that it self-improves via gradient signals extracted from `execution_traces`. The safety model assumes `execution_traces` is trustworthy (written by verified agents under scope constraints). At 10M/day, a compromised or misaligned agent writing maliciously crafted traces could systematically bias the gradient signal and steer promotion outcomes without ever triggering a single-event review.

Opus sketches 3 mitigations: (1) `safety_constraints` immutability (`0012_safety.sql`), (2) human sign-off at Rung 6, (3) `mutation-adversary` stress testing.

**My assessment: these are necessary but not sufficient.** Here's why:

- **Mitigation (1)** is a root-of-trust anchor, not a defense against poisoned *inputs* to that root. The immutability trigger protects the rules, not the gradient stream they're applied to.
- **Mitigation (2)** is rate-limited by human review bandwidth. At 10M task_runs/day, even 0.001% promotion rate = 100 promotions/day — far past the bandwidth of any single reviewer. The poisoning attack just needs to operate below whatever sampling rate the human review uses.
- **Mitigation (3)** is `lib/mutation-adversary.sh` operating at the recipe level. It does not adversarially perturb `gradient_records` rows directly — it perturbs upstream agent prompts. A patient attacker who *is* the agent can sidestep this entirely.

**What's missing.** A gradient-integrity verifier. Concretely: every `gradient_records` row should carry a verifiable provenance chain to the `execution_traces` row that produced it, signed by the agent's promoted-version hash. The `audit_log` append-only trigger gives the substrate; what's missing is the *checker* that periodically samples gradient rows, re-derives them from source traces, and flags drift. This is a 2–3 eng-week build, not a trivial config change.

**Recommendation**: track this as a P3 (research-mode) item now. It is not blocking 100K/day. It is the single biggest unresolved structural risk before any production 10M/day deployment.

---

## Section 6 — Dogfood Reflection (meta-loop check)

**Was this audit reproducible via the framework?** Yes. The 4 lens nodes fanned out under the `refactor-audit` recipe (`mini-ork run refactor-audit … --dispatch-mode parallel`). The lens-completeness verifier (`verifiers/lens-completeness.sh`) gated the publisher node, and this synthesis is itself a reviewer-class artifact under the canonical workflow.

**Did any lens get blocked by something the audit itself identified?** Two interesting feedback loops:

1. **Codex lens (D-1) flagged that `cl_opus.sh` pins all subagent slots to opus** — and Codex itself ran under a non-opus lane, so it caught the pattern in a way an opus-lane lens would not have flagged with the same urgency. Meta-loop holds: cheaper lenses see expensive-lane waste more clearly.

2. **GLM lens G-1 / Kimi K-1 / Codex D-2 all flagged the serial per-trace LLM loop in `lib/reflection_pipeline.sh:65`** — which is exactly the pattern that *this very audit* uses to fan out 4 sequential lenses. The audit pipeline itself ran serially in the orchestrator's `_flush_parallel_batch` (G-12), capping at batches of 4 with synchronous `wait`. If we'd wanted 8 lenses, we'd have run 4 then 4 — not a continuous sliding window of 8. So: the audit identified a bottleneck the audit-orchestration substrate also has.

**Was the audit itself within the cost budget?** Aggregate spend at run-end: see `cost-ledger.txt` (budget verifier `budget-cap` enforced `MO_REFACTOR_AUDIT_BUDGET_USD=$40` default). Lens-opus (1500–2500 word narrative on opus) was the largest single line item.

**Verifier-false-pass risk**: `verifiers/lens-completeness.sh` ships strict checks for file existence + non-empty + ≥1 file:line anchor + section-count gates. The depth-check (`glm-finding-count` requires 15–60 headings/list items, `opus-seven-sections` requires `## ` × 7 + `^[0-9]+\. ` × 8) blocks the stub-pass class of failure. The contract is sound; no obvious gap.

---

## Section 7 — How to Re-run This Audit

```bash
cd /Volumes/docker-ssd/ps/mini-ork
git checkout 6d70157   # planner-time HEAD (read-only invariant)

# Set budget cap (default $40); reduce for cheaper dry runs
export MO_REFACTOR_AUDIT_BUDGET_USD=40

# Optional: lower model effort for cheaper exploratory re-run
export MO_WORKER_EFFORT_LEVEL=medium

# Run
bin/mini-ork run refactor-audit \
    kickoff-prompts/scalability-audit.kickoff.md \
    --dispatch-mode parallel
```

Artifacts land in `~/.mini-ork/runs/run-${RUN_ID}/`:
- `lens-{glm,kimi,codex,opus}.md` — the 4 lens reports
- `synthesis.md` — this document
- `cost-ledger.txt` — per-node cost trail (gated by `budget-cap` verifier)

Publisher copies `synthesis.md` → `docs/refactor/SCALABILITY-AUDIT.md` and commits with message `audit(scalability): refresh from run-${RUN_ID}`. **Publisher will not run if `verifiers/lens-completeness.sh` failed** — fail-closed by design.

**P1 blocker on self-dispatch?** None of the Top-5 W1–W5 items block re-running this audit. W3 (`cl_opus.sh` fan-out) does inflate the cost of running it — applying W3 first would drop the next run's cost by ~50–60%. **Recommendation: ship W3 before the next refresh.**

---

## Recommended Next 3 Code-Fix Recipes

Ranked by ROI = (severity × leverage) / effort. Each maps to a single `code-fix` recipe invocation against a focused file set.

1. **`code-fix: w3-cl-opus-tier-split`** — patch `lib/providers/cl_opus.sh:11-14` to split sonnet/haiku/subagent model envs off opus. 15-minute patch, ~$8K/day saved at 100K tier. **Ship first.**

2. **`code-fix: w2-kill-d009-double-charge`** — delete or gate the D-009 flat-rate cost charge block in `bin/mini-ork-execute:718-737` now that D-029 records real per-node cost. Half-day patch + cost-ledger backfill script. Doubles effective daily budget headroom without raising the cap.

3. **`code-fix: w1-batch-gradient-extraction`** — rewrite `lib/reflection_pipeline.sh:65-73` from a serial per-trace LLM loop to a 20-trace batched dispatch + bulk `executemany` SQL insert (K-1's after-block). 3-day patch, ~95% LLM call-count reduction on reflect cycles, eliminates the framework's single largest hot-loop cost class.

These three, shipped in order, recover ~$14K/day of bleed and clear the prerequisite for Bundle A1 (persistent SQLite helper daemon) without any schema migration.
