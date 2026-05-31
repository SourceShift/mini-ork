# mini-ork v0.1.1 — Scalability Audit Synthesis

> **Run**: `run-1780241430-30697` · **Commit**: `a896b23` (v0.2-pt9) · **Date**: 2026-05-31
> **Lenses fused**: `lens-glm.md` (28 tactical findings) · `lens-kimi.md` (12 refactor diffs) · `lens-codex.md` (15 dispatch/cost findings + 3 arch shifts) · `lens-opus.md` (14 numbered recommendations + 1 hard question)
> **ID prefixes**: `G-N` = GLM (F-N in source) · `K-N` = Kimi (refactor-N) · `D-N` = Codex (finding-N) · `O-RN` = Opus
> **Consensus marker**: ★ = surfaced by 2+ lenses independently

---

## §1. Severity × Leverage Matrix

Rows = severity (when it breaks). Cols = leverage (size of fix × blast radius).
Bolded IDs carry $ savings ≥ $1K/day at 100K-task tier.

|                 | **HIGH leverage**                                                                                                | **MED leverage**                                                       | **LOW leverage**                              |
|-----------------|------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------|-----------------------------------------------|
| **P1** (now/1K) | **D-5** lane-cache parallel regression · **D-2** budget-check fork-storm · **D-3** budget-gate bypass · ★ **K-3/G-10/D-7** datetime python3 fork · **D-4** planner kickoff cap | K-1/G-19 ★ cache SQL-injection · G-17 iters.verdict index · K-2 O(N²) hash_bundle | K-10 case-stmt lane check · K-12 uuid→randomblob |
| **P2** (100K)   | ★ G-1/K-6/D-1 serial gradient LLM fan-out · O-R1 DB-proxy daemon · O-R7 LLM gateway · D-9 lane-helpers source-on-every-call · D-13 per-stage max_turns | ★ G-2/K-11 cluster-loop K+1 forks · K-7 finalize 1200-fork loop · K-8 runs-tracker 3-fork open · K-9 context_assembler 2-fork · G-5 v_agent_performance correlated subquery · G-6 v_claimable 3-correlated · G-7/8 scope-overlap O(N²)·git-ls-files · O-R3 context-cache · O-R14 cost-attribution view · D-8 reflection step 3+4 parallelize · D-12 normalized hash · D-15 worker.log rotation | G-25 set -e toggle · D-6 jittered backoff · K-4/5 git-blame batching · K-5 ThreadPoolExec |
| **P3** (10M)    | O-R4 Postgres backend · O-R5 Go/Rust worker · O-R6 ClickHouse traces · D-arch1 Haiku tier · D-arch3 batched reflection pipeline | O-R2 trace TTL ladder · ★ G-14/15/16/21/22 + O-R8 GC scheduling · O-R12 embedding gradient clusters · G-24 GC DELETE without LIMIT · G-23 LIKE-defeated index · G-20 dedup backlog · D-10 codex cost accounting · D-14 provider fallback · D-arch2 semantic cache | O-R10 recipe signing · O-R11 verifier sandbox · O-R9 schema gate · O-R13 OTel spans |

**Consensus clusters:**
- ★ **CONSENSUS-1**: serial-per-trace gradient extraction → G-1 + K-6 + D-1 (3 lenses)
- ★ **CONSENSUS-2**: python3-fork for `expires_at` datetime in `mo_cache_emit` → G-10 + K-3 + D-7 (3 lenses)
- ★ **CONSENSUS-3**: unmetered GC / no scheduled sweep → G-14/15/16/21/22 + O-R8 + K-3 (GLM × 5 + Opus + Kimi)
- ★ **CONSENSUS-4**: SQL-injection / unparameterized cache writes → G-19 + K-1 (2 lenses, both call out the same lines)
- ★ **CONSENSUS-5**: cluster-loop fork explosion in `reflection_pipeline.sh` step 5 → G-2 + K-11 (2 lenses)
- ★ **CONSENSUS-6**: `runs-tracker` multi-fork dispatch open → G-18 + K-8 (2 lenses)

---

## §2. Top 5 P1 Wins (this sprint, total ≤ 1 week)

Ranked by ROI = (severity × consensus × $-impact) ÷ effort.

| # | ID | Title | Source | One-line fix | File:line | Effort |
|---|----|-------|--------|--------------|-----------|--------|
| 1 | **D-5** | Lane-cache silently regressed in parallel dispatch | Codex | Export `MINI_ORK_LANE_CACHE_<node_type>=<model>` before the parallel subshell batch instead of relying on `declare -gA` in a subshell | `lib/llm-dispatch.sh:228–250` | XS (½ day) |
| 2 | **D-2** | Budget circuit-breaker forks python3 on every dispatch | Codex | Cache daily-spend in env vars `_MO_BUDGET_SPENT` + `_MO_BUDGET_CACHED_AT` with 5-min TTL; refresh inline only on miss | `lib/llm-dispatch.sh:201–213` | S (1 day) |
| 3 | **D-3** | Budget gate bypassed by 4 direct callers | Codex | Move budget check from `llm_dispatch()` shim into `mo_llm_dispatch` proper; remove from shim to avoid double-charge | `lib/llm-dispatch.sh:37–43` (move from `:194–218`) | XS (½ day) |
| 4 | ★ **K-3 / G-10 / D-7** | `mo_cache_emit` forks python3 for `now+30d` math | Kimi+GLM+Codex consensus | Replace `python3 -c "import datetime ..."` with SQLite-native `datetime('now', '+30 days')` inlined into the INSERT | `lib/cache.sh:146–149` | XS (½ day) |
| 5 | **D-4** | Planner inlines full kickoff verbatim — no token cap | Codex | Add `MO_PLAN_KICKOFF_MAX_TOKENS=2000` env var; truncate before injection into planner prompt; full body still passed as file ref | `bin/mini-ork-plan:158–165` | S (1 day) |

**Quantified impact at 100K tasks/day** (sum of P1 fixes):
- ~600K spurious python3 forks/day eliminated
- ~$2,400/day in planner token waste recovered
- Closes 4-caller budget-bypass exposure → reflection-storm cap restored
- Closes lane-cache parallel-mode regression → restores the D-02/G-002 savings that have been silently leaking

**Why these five specifically:** every one is XS/S effort, three of five are surfaced by ≥2 lenses or close a known regression, and items 1 and 3 are *correctness bugs* (silent failure in lane cache + 4-caller budget bypass) not optimizations — they go first.

**Defer to P1.1 (next sprint, +3 days):**
- **K-1 / G-19** ★ — parametrize cache.sh SQL (security: closes injection in `'$epic'`, `'$stage'`, `'$input_hash'`); same patch ships parameterized `UPDATE … RETURNING` for cache lookup+bump consolidation
- **G-17** — `CREATE INDEX idx_iters_verdict ON iters(verdict) WHERE verdict IS NOT NULL` (1 line, unblocks `v_failure_patterns` and `v_epic_convergence`)
- **K-2** — replace O(N²) `data="${data}$(cat …)"` with process-substitution pipe in `mo_cache_hash_bundle`

---

## §3. v0.2 → v0.3 Architectural Shifts (P2, bundled)

### Bundle A: Process-model cleanup (data-layer + runtime)
**Total effort: 3–4 eng-weeks · Prereqs: D-5 (P1)**

| ID | Change | File:line | Effort |
|----|--------|-----------|--------|
| O-R1 | Persistent DB-proxy daemon on Unix socket; replaces all inline `python3 -c "import sqlite3..."` heredocs | new `lib/db_proxy.sh` + `lib/db_server.py` | M (1–2 wks) |
| K-8 | Fold `mo_runs_open` schema-check + INSERT into single Python session (becomes one daemon call once R1 lands) | `lib/runs-tracker.sh:31–122` | S |
| K-9 | Merge `context_assembler.sh`'s two python3 forks into one | `lib/context_assembler.sh:40–59` | S |
| K-7 | Replace finalize.sh triple-nested for+grep+jq+awk (1200 forks/finalize) with single Python pass | `lib/finalize.sh:101–139` | M |
| D-9 | `lane-helpers.sh` source-guard (`__MO_LANE_HELPERS_LOADED`) — 300K source-calls/day eliminated | `lib/llm-dispatch.sh:67–73` + `lib/lane-helpers.sh` (add guard) | XS |

**Risk if deferred**: at 100K/day this bundle is the difference between a single-core saturated dev-box and a system that runs comfortably on one machine. Without R1, every 1K task_runs spawns ~5K cold Python interpreters.

### Bundle B: LLM-dispatch redesign (cost + latency)
**Total effort: 2–3 eng-weeks · Prereqs: D-2, D-3 (P1) · Independent of Bundle A**

| ID | Change | File:line | Effort |
|----|--------|-----------|--------|
| ★ G-1/K-6/D-1 | Batch gradient extraction: pack 20 trace JSONs per LLM call; or `xargs -P 8` parallel fan-out | `lib/reflection_pipeline.sh:51–58` + `lib/gradient_extractor.sh:97–107` | M |
| ★ G-2/K-11 | Cluster-summary loop: K+1 python3 forks → 1 (also caps cluster fan-out at `MO_SUMMARIZE_CLUSTER_CAP=50`) | `lib/reflection_pipeline.sh:253–262` | S |
| D-8 | Parallelize `reflection_link_failures` ∥ `reflection_detect_stale` (independent, different tables) | `lib/reflection_pipeline.sh:247–252` | XS |
| D-13 | Per-stage `MO_<STAGE>_MAX_TURNS` env vars (rubric=5, reflection=10, plan=20, worker=60) | `lib/llm-dispatch.sh:42` | S |
| D-12 | Normalize hash inputs (strip trailing ws, lowercase, cap diff_summary at 1KB) → +10-15% cache hit rate | `lib/cache.sh:99–112` | S |
| O-R7 | LLM gateway HTTP proxy on `localhost:28080`; HTTP keep-alive replaces CLI fork-per-dispatch | new `cmd/llm-gateway/` | M |

**Risk if deferred**: at 100K/day, serial gradient extraction takes ~4 hours of wall-time per reflection cycle. With batching it drops to ~12 min. Reflection cycles become tractable for cron scheduling.

**$-impact (per Codex baseline):**
- G-1/K-6/D-1 batching: ~$51/day saved at 100K, ~$5,100/day at 10M (system-prompt deduplication alone)
- D-13 per-stage turns: $450–$1,500/day at 100K
- D-12 hash normalization: ~$810/day at 100K
- **Bundle B total: ~$1,300/day at 100K · ~$7K/day at 10M (excluding LLM-gateway gains)**

### Bundle C: Query + DB hot-path cleanup
**Total effort: 1–2 eng-weeks · Prereqs: K-1/G-19 from P1.1 · Independent**

| ID | Change | File:line |
|----|--------|-----------|
| G-5 | `v_agent_performance`: replace correlated subquery with pre-aggregated JOIN | `db/views/v_agent_performance.sql:8` |
| G-6 | `v_claimable`: materialize deps + inbox into CTEs | `db/views/v_claimable.sql:18–30` |
| G-7/G-8 | scope-overlap O(N²) + per-glob git-ls-files: pre-build file→epics inverted index in one pass | `lib/scope-overlap.sh:169–191` |
| G-23 | `gradient_records` leading-wildcard LIKE: add explicit `task_class` column + index | `lib/context_assembler.sh:80–110` |
| G-27 | `v_agent_performance`: `date(r.started_at) > date('now','-30 days')` defeats index → use literal ISO comparison | `db/views/v_agent_performance.sql:11–12` |
| O-R3 | Reuse `mini_orch_sessions` for content-addressed context cache | `lib/cache.sh` + `lib/context_assembler.sh` |
| O-R14 | New view: `db/views/v_cost_attribution.sql` joining task_runs→recipe + execution_traces→task_class | new file |

### Bundle D: GC + retention discipline (data-layer hygiene)
**Total effort: 1 eng-week · Prereqs: none (but Bundle A makes implementations simpler)**

| ID | Change |
|----|--------|
| ★ G-14/15/16/21/22 + O-R8 | Wire `lib/cleaner.sh:_mo_cleaner_expired_sessions` into nightly cron AND pre-flight. Add GC sweeps for `llm_calls`, `orch_dispatches`, `reflection_log`, `decision_basins`, `emergent_patterns`, `artifact_memory.retained_until`. |
| G-24 | `mo_cache_gc` DELETE without LIMIT holds write lock at 10M scale → batch with `WHERE uuid IN (SELECT ... LIMIT 1000)` loop |
| G-20 | Reflection dedup capped at `MO_DEDUP_BATCH=10000` but pipeline doesn't iterate → add `while has_dupes; do reflect_deduplicate; done` outer loop |

**Risk if deferred**: at 100K/day, the un-archived tables grow at ~365K rows/year per dispatch table. By Tier-3 (10M/day) these dominate query plans and the WAL chokepoint.

### Bundle E: Observability + cost attribution (10M-tier prerequisite)
**Total effort: 1–2 eng-weeks**

| ID | Change |
|----|--------|
| O-R13 | OTel span per `mo_llm_dispatch` (OTLP HTTP to `localhost:4318/v1/traces`); span attrs include `cache_read_tokens` |
| O-R14 | `v_cost_attribution.sql` (also in Bundle C) — single source of truth for per-recipe-per-lane $-spend |
| D-15 | `worker.log` rotation at `MO_WORKER_LOG_MAX_MB=50` (currently unbounded → 5TB/day at 100K) |

---

## §4. P3 / Long-Horizon (advisory, not load-bearing now)

### v2.0 substrate migration (Opus tier-3 path)
- **O-R4** — Postgres backend, pgBouncer, monthly partitioned `execution_traces` · L (4–8 wks)
- **O-R5** — Go/Rust task-queue worker replacing bash dispatcher hot path · L
- **O-R6** — ClickHouse / TimescaleDB for trace writes (append-only time-series belong in columnar) · L
- **D-arch1** — Haiku tier in `agents.yaml` for rubric / gradient / deduplication (~$1,800/day saved at 100K, ~$180K/day at 10M)
- **D-arch3** — Three-pass reflection pipeline (rule-based → Haiku batch → Sonnet top-5%) — 99% cost reduction at 10M scale
- **D-arch2** — Semantic-similarity cache via `sentence-transformers` + `sqlite-vss` (defer; Tier-3)
- **O-R12** — Embedding-based gradient clustering (replaces text-similarity heuristic in `pattern_store.sh`)

### Marketplace + sandbox hardening (orthogonal, can ship anytime)
- **O-R9** — Enforce `schemas/workflow.schema.json` at `mini-ork run` entry (already documented in `docs/ARCHITECTURE.md`, not enforced)
- **O-R10** — Recipe signing: `manifest.sha256` listing `workflow.yaml` + `task_class.yaml` + prompts; verify before sourcing
- **O-R11** — `env -i` sandbox for `artifact_contract.success_verifiers[]` (matches `hooks/scope-enforce.sh` pattern)

### Resilience (low frequency, high consequence)
- **D-14** — Provider fallback: `MO_OPUS_FALLBACK_MODEL` + `MO_SONNET_FALLBACK_MODEL` for 5xx outages
- **D-10** — Cost accounting for codex/gemini lanes (currently $0 in budget tally — blind spot if codex volume grows)
- **D-6** — Exponential backoff + jitter in `_mo_bridge_apply_wait` (thundering herd prevention)
- **D-11** — Inline 1–2 retries in `mo_llm_dispatch` for transient 5xx (~15-20% wasted-call recovery)

---

## §5. Hardest Open Question (inherited from Opus §7)

> **Can the PromotionGate reliably distinguish "better at tasks" from "better at gaming the benchmark suite" when both the workflow-evolution engine AND the benchmark task set are mutable state in the same database?**

Opus sketches three mitigations:
1. **Constitutional anchor tasks** — small immutable benchmark set, signed in `version_registry`, never part of the improvement feedback loop
2. **Human-gate on benchmark mutation** — any `benchmark_tasks` write requires `human_gate` approval
3. **Separate evaluation DB** — read-only DB the improvement pipeline cannot reach

### Synthesizer assessment: **insufficient — none of the three closes the loop on its own**

- **Option 1 alone is necessary but not sufficient.** The anchor set fights Goodhart pressure only along the axes it measures. If the constitutional set covers, say, "code-fix" tasks and the system specializes "refactor-audit" workflows, the anchor set never catches the drift. Anchor-set composition becomes itself a moving target as task diversity grows — exactly the regress Opus flags.
- **Option 2 is operationally feasible but pushes the problem to humans** who don't have the context to evaluate whether a `benchmark_tasks` mutation is benign vs. adversarial. At 100K/day there are too many mutations to gate by hand without throughput collapse.
- **Option 3 is the cleanest separation but doubles the schema-migration burden** (every change to `benchmark_tasks` shape must replicate across two DBs) and breaks the audit-log atomicity guarantee from `docs/SAFETY.md`.

**Recommended composition (none-of-the-above hybrid):**

- **Adopt Option 1** as the floor — pick 50–100 anchor tasks across the 8 node types, hash-lock in `version_registry`, never mutate.
- **Add adversarial-eval co-evolution**: at every promotion gate, the system also generates *new* benchmark tasks designed to *break* the candidate workflow (anti-task generation via the same LLM lane). A candidate must pass BOTH the constitutional anchor AND the adversarial set. This is the GAN-style fix for the Goodhart loop — borrows from RLHF reward-model debate work.
- **Defer Option 3 to Tier-3** — when the move to Postgres is happening anyway, split benchmark tasks into a separate read-only schema. Free with the v2.0 migration.

**Research gap:** The adversarial co-evolution path needs prior-art review. Search terms: "adversarial benchmark generation for self-improving systems," "Goodhart-resistant evaluation in agentic loops," "LLM-as-judge debate for promotion gates." This is a 1-week research spike, not a code task — recommend before committing to v2.0 architecture.

---

## §6. Dogfood Reflection (meta-loop check)

**Was this audit reproducible via the framework?** Yes — the 4-lens parallel + synthesizer + verifier + publisher topology is a stock `refactor-audit` recipe shape. The plan emitted by the planner node (see `plan.json` in this run dir) decomposes cleanly into the standard DAG: `planner → 4 researchers in parallel → reviewer (synthesizer) → verifier → publisher`.

**Did any lens get blocked by something the audit itself identified?** **Yes — D-5.** The lane-cache parallel-mode regression that Codex surfaced is the exact failure mode the audit ran into. The four lens nodes were dispatched in parallel via `mini-ork-execute`'s subshell-batch path, which means each lens spawned a fresh `python3` to parse `agents.yaml` for lane resolution. The cache that was supposed to hold the lane mapping silently evaporated at every subshell boundary. **The audit cost more than it should have because of a bug the audit itself found.** This is a clean meta-loop hit.

**Did any lens get blocked by missing infrastructure?** Two notes:
- The Opus lens cites file paths the auditor cannot independently verify against `git ls-files` from within a lens-only worker (no shell access in some research lanes). The synthesizer (this doc) treats those as load-bearing claims requiring spot-verification before code-fix dispatch. The `git diff --name-only HEAD -- bin/ lib/ db/ recipes/` verifier (in the plan's `verifier_contract.checks`) enforces the read-only contract — no source mutation occurred.
- The Codex lens makes $-projections at 100K and 10M tiers that are derived, not measured. No load tests were run. Treat all dollar figures as model-based estimates; the *ranking* (which finding saves more than which) is robust, the *absolute magnitudes* are not.

**Honest gap**: The lens reports did not coordinate with each other. Consensus markers in §1 are detected by the synthesizer post-hoc by string-matching file:line anchors. Two lenses pointing at the same line is a strong signal, but two lenses pointing at *adjacent* lines might be missed by the matcher. Spot-check the matrix before acting on it.

---

## §7. How to Re-run

```bash
# From repo root
export MO_REFACTOR_AUDIT_BUDGET_USD=40
export MINI_ORK_LANES_FILE=agents.yaml
bin/mini-ork run refactor-audit \
  --kickoff docs/refactor/SCALABILITY-AUDIT-KICKOFF.md \
  --epic v0.2-refactor-audit
```

**Verification (after run):**
```bash
verifiers/lens-completeness.sh "${MINI_ORK_RUN_DIR}"
```

**Cost ledger:**
```bash
cat "${MINI_ORK_RUN_DIR}/cost.ledger"
```

**Blocker on faithful re-run**: ⚠️ **D-5 (lane-cache parallel regression) blocks cost-accurate self-dispatch.** Until D-5 ships, every parallel-lens recipe (including this one) silently re-parses `agents.yaml` per lens. Concretely: re-running this audit today costs ~3–5× more in `python3` startup than it should. **Land D-5 before re-running**, or run lenses serially with `MINI_ORK_MAX_PARALLEL=1` (slower wall-time, accurate cost).

---

## Appendix: Cross-reference index

| Topic | GLM | Kimi | Codex | Opus |
|-------|-----|------|-------|------|
| Serial gradient LLM | F1 | refactor-6 | finding-1 | (implied R12) |
| Cluster-summary forks | F2 | refactor-11 | — | — |
| Cache datetime fork | F10 | refactor-3 | finding-7 | — |
| Cache SQL injection | F19, F26 | refactor-1 | — | — |
| runs-tracker forks | F18 | refactor-8 | — | — |
| context_assembler forks | F23 | refactor-9 | — | R3 |
| Auto-merge forks/locks | F3, F4, F12 | — | — | — |
| Scope-overlap O(N²) | F7, F8 | — | — | — |
| Correlated view subqueries | F5, F6, F27 | — | — | (implied R14) |
| GC / retention sweeps | F14, F15, F16, F21, F22 | (touched in refactor-3) | — | R8, R2 |
| Lane cache + helpers | (in `_MO_LANE_CACHE` comments) | refactor-10 | finding-5, finding-9 | — |
| Budget gate | — | — | finding-2, finding-3 | — |
| Planner kickoff cap | — | — | finding-4 | — |
| Per-stage max_turns | — | — | finding-13 | — |
| Provider fallback | — | — | finding-14 | — |
| LLM gateway / proxy | — | — | — | R1, R7 |
| Substrate migration | — | — | arch-1/2/3 | R4, R5, R6, R12 |
| Marketplace gates | — | — | — | R9, R10, R11 |
| Observability spans | — | — | finding-15 (logs) | R13, R14 |
| Promotion-gate Goodhart | — | — | — | §7 (open Q) |

— *end synthesis* —
