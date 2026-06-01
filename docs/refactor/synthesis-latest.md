# mini-ork v0.1.1 — Scalability Audit Synthesis

> **Audit run:** `run-1780296925-74626` · **Date:** 2026-06-01
> **Lenses:** GLM (tactical), Kimi (refactor), Codex (LLM-dispatch), Opus (architectural)
> **Target scales:** 1K/day (current) → 100K/day → 10M/day
> **Lineage:** Supersedes any prior `docs/refactor/SCALABILITY-AUDIT.md`.
>
> **ID conventions:** `G-N` = GLM, `K-N` = Kimi, `D-N` = Codex (the lens emitted
> `C-N` internally; we re-prefix here per framework convention), `O-RN` = Opus.
> A `★` marker plus a `[CONSENSUS-N]` tag indicates the finding surfaced in
> ≥2 lenses independently.

---

## 1. Severity × Leverage Matrix

| | **HIGH leverage** | **MED leverage** | **LOW leverage** |
|---|---|---|---|
| **P1 — blocks NOW / blocks 100K** | G-01 (raw sqlite3 in hooks/subagent-stop.sh:68) · G-02 (no circuit on `mo_llm_dispatch`) · G-03 (budget SUM misses `runs` table) · **★ [CONSENSUS-1]** G-08 + K-04 + D-006 + O-R12 (budget-check fork storm) · **★ [CONSENSUS-3]** G-05 + K-01 + D-002 + O-R9 (N+1 gradient extraction) · D-003 (Opus provider tier cascade) | G-06 (gradient_records.evidence unindexed) · G-07 (agent_registry index gap) · D-004 (cache wired to only 2 of 9 stages) · **★ [CONSENSUS-4]** G-04 + K-07 (unbounded failure fetchall — OOM bomb) | G-15 (iters.verdict index) · G-18 (runs.started_at index) · K-02 (python3 fork for date math) · K-12 (PLAN_CONTENT hoist) |
| **P2 — blocks 10M / structural** | O-R1 (Go DB proxy daemon) · O-R2 (Go worker replaces bash DAG) · **★ [CONSENSUS-5]** G-19 + O-R4 (no TTL/archive on `execution_traces`, `task_runs`) · D-008 (Haiku tier for classification) · D-002-long (batch gradient extraction → 94 % LLM cost cut) | O-R3 (SQLite monthly shards via ATTACH) · O-R13 (OTel spans/metrics/logs) · **★ [CONSENSUS-2]** G-12 + K-08 + D-012 (per-cluster python3 storm in reflection step 5) · D-009 (context-pack 64 K bloat) · D-011 (full trace JSON in gradient prompt) · K-06 (git HEAD cache) · G-14 (auto-merge spin-lock) | G-16 / G-17 (correlated subqueries + JSON shred in views) · G-20 (v_memory_health 8-arm UNION) · K-09 (merge d021+d022 UPDATEs) · K-10 (mo_cache_hash_bundle streaming) · K-11 (git-blame batching) · D-014 (`--include-partial-messages` always on) |
| **P3 — long-horizon / advisory** | O-R4 (Postgres 16 + pg_partman migration) · O-R5 (NATS JetStream queue + stateless workers) · O-R11 (committee reviewer at 1 M/day) | O-R6 (pgloader migration script) · O-R7 (recipe registry + semver tap) · O-R8 (namespace sandbox for verifier scripts) · O-R10 (tiered benchmark sampling by rung) · D-005 (effort-level complexity routing) · D-007 (per-stage max_turns routing) | K-03 (raw SQL string interpolation in cache.sh) · D-001-NEW (cleaner.sh missing prompt cache) · D-010 (reviewer on empty worker output) · D-013 (flat-sleep retry storm) · D-015-NEW (BDD runner serial by default) · G-23 (per-row python3 in utility_function.sh) · G-24 (agent_session_locks TTL prune) |

**Consensus catalog**

| Tag | Title | Lenses | File anchor |
|---|---|---|---|
| **[CONSENSUS-1]** | Budget circuit breaker spawns 2 × python3/dispatch | GLM, Kimi, Codex, Opus | `lib/llm-dispatch.sh:201-214` |
| **[CONSENSUS-2]** | Reflection step 5: cluster summarisation serial per cluster | GLM, Kimi, Codex | `lib/reflection_pipeline.sh:256-265` |
| **[CONSENSUS-3]** | Gradient extraction: 1 LLM call per trace, no idempotency, no batch | GLM, Kimi, Codex, Opus | `lib/reflection_pipeline.sh:53-63` + `lib/gradient_extractor.sh:111` |
| **[CONSENSUS-4]** | `reflection_link_failures` unbounded fetchall — OOM at 10 M rows | GLM, Kimi | `lib/reflection_pipeline.sh:127-130` |
| **[CONSENSUS-5]** | No rotation / TTL / archive on `execution_traces`, `task_runs` | GLM, Opus | `db/migrations/0013_task_runs.sql`, `db/migrations/0014_execution_traces*.sql` |

---

## 2. Top 5 Immediate Wins (P1) — total < 2 weeks

| Rank | ID | Title | Source | One-line fix | Effort |
|---|---|---|---|---|---|
| **1** | **★ [CONSENSUS-1]** (G-02 + G-03 + G-08 + K-04 + D-006) | Budget circuit-breaker correctness + fork-storm | 4 lenses | (a) move circuit check into `mo_llm_dispatch()` body so direct callers cannot bypass; (b) extend SUM to `task_runs + runs`; (c) collapse 2 python3 forks → 1 with `awk` float compare. `lib/llm-dispatch.sh:37,198-214` | 4 h |
| **2** | D-003 | Opus provider cascades Haiku/Sonnet → Opus 4.7 (~60× over-bill) | Codex | Remove `ANTHROPIC_DEFAULT_{HAIKU,SONNET}_MODEL` exports in `lib/providers/cl_opus.sh:10-14`. **$52/1K-runs saved.** | 15 min |
| **3** | **★ [CONSENSUS-3] (short-term)** (D-002 + G-06) | Gradient extraction: skip-if-done + index | 4 lenses | (a) `WHERE trace_id NOT IN (SELECT DISTINCT evidence FROM gradient_records)` in `lib/gradient_extractor.sh`; (b) `CREATE INDEX idx_gr_evidence ON gradient_records(evidence)`. **$135/1K-runs saved (60–80 % re-extraction).** | 1 d |
| **4** | D-004 | Wire `mo_cache_lookup`/`mo_cache_emit` to spec-author, spec-reviewer, reflection-refiner | Codex | Mirror the existing rubric/mutation-adversary cache pattern; schema already supports it (`lib/cache.sh:19-58`). **$38/1K-runs saved.** | 3 d |
| **5** | **★ [CONSENSUS-5]** (G-19 + G-21 + G-24) | Rotation: `execution_traces` 90-day purge + run-dir archive + `agent_session_locks` TTL prune | GLM, Opus | New migration 0015 + `mini-ork prune --days 90`. Retains failures for gradient signal. Prevents 10 M-row WAL bloat **before** R3 sharding is ready. | 2 d |

**Total:** ~6.5 eng-days. Apply as one PR-stack (`fix-v0.2-pt12-scalability-quickwins`). Estimated combined savings ≥ **$225/1K-runs** plus OOM-elimination and 200 K python3 forks/day removed.

---

## 3. v0.x+1 Architectural Shifts (P2) — bundled by theme

### Bundle A — **Data layer** (prereq for everything else at 100 K/day)

- O-R3 SQLite monthly shards via `ATTACH DATABASE` (`db/migrations/0013_task_runs.sql`, `0014_execution_traces*.sql`) — 1–2 wks
- **★ [CONSENSUS-5]** G-19 + G-21 cron-driven archive job to `.mini-ork/archives/` — already in P1 quick-wins as the immediate slice, completed here
- G-15 + G-18 view indexes (`idx_iters_verdict`, `idx_runs_started_agent`) — 0.5 wk
- G-13 fix `gradient_records.target` leading-`%` wildcard scan — 0.5 wk

**Bundle total:** 3 eng-wks. **Prereq P1s:** quick-win #5 (rotation). **Risk if deferred:** SQLite WAL writer ceiling becomes a hard wall around 80 K writes/day on dev hardware; views become seconds-slow.

---

### Bundle B — **Runtime** (process-spawn overhead — the real 10 K → 100 K gap)

- O-R1 Go DB proxy daemon over Unix socket (`lib/db_open.sh:25`, `lib/trace_store.sh:35-80`) — 2–3 wks
- K-06 cache `_mo_git_head` + `_mo_repo_root` (`lib/memory.sh:39-50`) — 0.5 wk
- K-09 merge `_d021_set_status` + `_d022_charge_node_cost` (`bin/mini-ork-execute:189-233`) — 0.5 wk
- **★ [CONSENSUS-2]** G-12 + K-08 + D-012 single-session cluster summariser (`lib/reflection_pipeline.sh:256-265`) — 0.5 wk
- G-23 consolidate per-row python3 forks behind `mo_json_get` jq helper — 0.5 wk
- G-14 auto-merge exponential backoff (`lib/auto-merge.sh:43-55`) — 0.5 wk

**Bundle total:** 4–5 eng-wks. **Prereq P1s:** **★ [CONSENSUS-1]** (budget circuit) — once cost path is fork-free, the DB proxy is the next leverage point. **Risk if deferred:** at 100 K runs/day with 8 nodes each, you are spending 2 + CPU-hours/day on pure spawn overhead. Bash `wait -n` semantics break above ~128 concurrent jobs.

---

### Bundle C — **LLM dispatch** (where the dollars are)

- **★ [CONSENSUS-3]** D-002-long batch gradient (N traces → 1 call, packs ~500 summaries) — 1–2 wks. **94 % cost cut at scale.**
- D-008 Haiku tier for gradient/rubric/classifier paths (`config/agents.yaml`, `lib/gradient_extractor.sh:109`) — 1 wk. **$138/1K-runs.**
- D-009 trim context-pack: prior_runs 10 → 3, strip verbose fields (`lib/context_assembler.sh:35,86-104`) — 0.5 wk. **$120–960/1K-runs.**
- D-011 pre-summarise trace JSON before gradient prompt (`lib/gradient_extractor.sh:106`) — 0.5 wk
- D-005 effort-level complexity routing (`bin/_worker-launcher.sh:236`) — 1 wk
- D-007 per-stage `max_turns` routing — 0.5 wk
- D-013 exponential backoff + retry-count tracking in `wait-and-retry` (`lib/mo-healer-bridge.sh:181-187`) — 0.5 wk
- D-010 short-circuit reviewer on empty worker diff — 0.25 wk

**Bundle total:** 5–6 eng-wks. **Prereq P1s:** D-004 (cache wiring) — semantic uplifts in D-009/D-011 land cleanly only after caching is universal. **Risk if deferred:** at 100 K/day this bundle is the difference between ~$2K and ~$15K daily Anthropic spend.

---

### Bundle D — **Observability** (do this FIRST, in parallel with A/B/C)

- O-R13 OTel spans/metrics/logs from Go worker (or bash via OTLP exporter sidecar) — 2–3 wks
- O-R12 in-process rolling 24 h cost counter (depends on R1/R2) — < 1 wk
- D-014 gate `--include-partial-messages` behind `MO_DEBUG=1` — 0.25 wk (saves $200/day storage at 100 K)
- K-10 streaming `mo_cache_hash_bundle` (`lib/cache.sh:78-89`) — 0.25 wk

**Bundle total:** 3 eng-wks. **Prereq P1s:** none — start immediately. **Risk if deferred:** every other refactor lands blind; you cannot prove a regression-free migration without traces.

---

**Recommended sequencing:** Bundle D in parallel with quick-wins (week 1–2), then A → B → C (weeks 3–14). Total **15 eng-weeks** to a substrate that holds at 100 K/day.

---

## 4. Long-horizon (P3 + advisory)

| ID | Item | Trigger | Effort | Notes |
|---|---|---|---|---|
| O-R2 | Go worker replaces `bin/mini-ork-execute` bash DAG | 100 K/day plateaus | 6–10 wks | Highest-leverage structural change; gate on Bundle D being live first |
| O-R4 | Postgres 16 + `pg_partman` migration | crossing 1 M/day | 8–12 wks | All 14 migrations have clean Postgres equivalents; `audit_log` triggers must port unchanged |
| O-R5 | NATS JetStream task queue + stateless workers | crossing 1 M/day | 4–6 wks | Requires object-storage replacement for `MINI_ORK_RUN_DIR` |
| O-R6 | `db/migrate-to-pg.sh` via `pgloader` | concurrent with R4 | 1–2 wks | |
| O-R7 | Recipe registry with semver + content hash | community contributions open | 3–4 wks | Homebrew-style tap model |
| O-R8 | Namespace sandbox for verifier scripts (`unshare` / `sandbox-exec`) | community contributions open | 2–3 wks | Security-critical; blocks R7 publication |
| O-R10 | Tiered benchmark sampling by mutation rung | 100 K/day | 1 wk | rung ≤ 4 → 20 % weighted sample; rung ≥ 5 → full suite |
| O-R11 | Committee reviewer gate (2-of-3 multi-provider quorum) | 1 M/day, human becomes bottleneck | 2–3 wks | Preserves safety contract w/o single-human gate |
| O-R9 | MinHash LSH dedup on `textual_gradients` | gradient store > 100 K rows | 1 wk | Prevents GroupEvolver noise saturation |
| K-03 | Parameterised cache queries (drops SQL-injection risk) | non-trusted recipe inputs | 0.5 wk | Currently low-impact (epic IDs are framework-controlled); high-impact under R7 |
| K-11 | Batch git-blame per file in `_mo_capture_reflection` | memory writes > 10 K/day | 1 wk | Saves ~500 K forks/day at 100 K writes |
| D-001-NEW | Wire `mo_emit_cache_flags` in `lib/cleaner.sh:299` | low priority — single non-cached site remaining | 0.25 wk | $3.60/1K-runs |
| D-015-NEW | Parallel BDD runner default-on | sub-epic count > 5/job | 0.25 wk | 27 min/job wall-clock |
| G-23 | jq-based `mo_json_get` helper | python3 fork count > 10 K/day | 0.5 wk | Already covered by Bundle B but listed for completeness |
| G-24 | `agent_session_locks` expired-row purge | session count > 10 K | 0.25 wk | Index already exists (`idx_agent_session_locks_expires`); cursor is missing |
| G-20 | Snapshot `v_memory_health` counts instead of 8-arm UNION | doctor command latency complaint | 1 wk | Cosmetic until 10 M rows/namespace |

---

## 5. Hardest Open Question

**Inherited from Opus §8.** The PromotionGate utility formula

```
U = 0.45·success_rate + 0.20·verifier_pass + 0.15·artifact_quality
  − 0.10·cost − 0.05·latency − 0.05·risk_penalty
```

has fixed weights and a benchmark suite that is anchored to the *original*
task-class distribution. When production traffic shifts — say `db_migration`
grows from 5 % to 60 % of runs — the benchmark suite still evaluates
candidates mostly on the old distribution. A workflow change that is highly
beneficial for the new dominant class shows near-zero `utility_delta` and is
wrongly quarantined. Worse, the self-improvement loop *learns* this bias
and stops proposing changes for the dominant class.

Opus sketched three mitigations: (a) continuously refresh the benchmark
suite, (b) stratify by task class and require `utility_delta > 0` per
stratum, (c) Pareto-dominance instead of scalar comparison.

**My assessment:** none of the three is sufficient on its own.

- **(a)** sacrifices longitudinal comparability — `utility_delta` measurements
  taken six months apart become incommensurable. You lose the ability to
  detect slow regressions.
- **(b)** fails the moment a candidate improves stratum X by 5 σ but
  hurts stratum Y by 0.1 σ. Under strict per-stratum gating, you reject every
  generalist improvement.
- **(c)** has no unique answer when strata trade off — and the self-evolution
  loop needs a *decision*, not a Pareto frontier.

The right answer is almost certainly a **hybrid** that the audit framework is
not equipped to specify without more research:

1. Per-stratum tracking (b) gives the substrate.
2. A traffic-weighted aggregate, where weights are **smoothed** versions of
   recent production distribution (e.g. 30-day EMA), gives a single scalar
   for promotion gating — without the moving-target problem of (a) because
   the comparison version's score is recomputed under the *same* current
   weights.
3. A separate **regression detector** that watches each stratum independently
   and triggers human review on any stratum-level utility drop > 2 σ —
   regardless of the aggregate.
4. The benchmark suite itself needs an explicit `coverage_target` per
   task class, refreshed quarterly from production telemetry.

This still does not solve the meta-problem (self-improvement loop learning
the bias of its own gate), which probably needs an off-policy evaluation
layer borrowed from offline RL — i.e. counterfactual utility estimates that
do not require running the candidate against the benchmark. **Further
research required**; the audit framework's recommendation is to ship Bundle D
(observability) so this question can be empirically studied as soon as
production task-class distributions start shifting.

---

## 6. Dogfood Reflection (meta-loop check)

**Was the audit itself reproducible via the framework?** Yes — the run
artifacts under `${MINI_ORK_RUN_DIR}` (`lens-glm.md`, `lens-kimi.md`,
`lens-codex.md`, `lens-opus.md`, this `synthesis.md`) are all generated
under the framework's `plan → dispatch → verify → publish` lifecycle. The
4 lens reports were dispatched in parallel from the same `plan.json`;
total cost remained inside the `MO_REFACTOR_AUDIT_BUDGET_USD=40` envelope
(see `.last-llm-cost`).

**Did any lens get blocked by something the audit ITSELF identified?**
Three meta-loop hits:

1. The audit's own cost telemetry is undercounted because of **G-03**
   (`SUM(task_runs.cost_usd)` excludes the `runs` table). The
   `MO_REFACTOR_AUDIT_BUDGET_USD` envelope check is therefore lenient by
   exactly the amount of cost charged to the `runs` table. The audit
   surfaced the bug it was simultaneously victim to.

2. **★ [CONSENSUS-1]** budget circuit breaker spawns 2 python3 forks per
   dispatch. The audit's 5 dispatch nodes (4 lenses + 1 synthesizer)
   triggered ~10 extra python3 forks for the budget check — measurable in
   the run log but cost-irrelevant at this scale. It would matter if the
   audit were itself run at 100K/day.

3. The audit lenses ran read-only against `bin/`, `lib/`, `db/`,
   `recipes/` — confirmed by `git status --porcelain` on those paths. The
   audit framework's read-only discipline held; this should be promoted to
   a hard sandbox (per O-R8) rather than a per-prompt convention.

**Recommendation:** add a **substrate self-audit step** to the framework
that runs the audit recipe quarterly against itself. Promote the audit
prompt-templates (`recipes/refactor-audit/`) to first-class versioned
recipes under the O-R7 registry once that exists.

---

## 7. How to Re-run

The plan that generated this synthesis lives at
`${MINI_ORK_RUN_DIR}/plan.json`. Re-execution path:

```bash
# from repo root
export MO_REFACTOR_AUDIT_BUDGET_USD=40
bin/mini-ork run recipes/refactor-audit/workflow.yaml \
    --target "$(pwd)" \
    --dispatcher parallel \
    --lenses glm,kimi,codex,opus
```

Outputs land in `.mini-ork/runs/run-<ts>-<pid>/`:

- `lens-glm.md`, `lens-kimi.md`, `lens-codex.md`, `lens-opus.md`
- `synthesis.md`
- `verifiers/lens-completeness.sh` exit code

The publisher then copies `synthesis.md` → `docs/refactor/SCALABILITY-AUDIT.md`
byte-for-byte (`diff -q` is one of the verifier checks).

**Blocking caveat:** the synthesizer in this run was dispatched via
`mo_llm_dispatch`, which **★ [CONSENSUS-1]** identifies as fork-leaky and
cost-undercounted. The audit can still self-dispatch — none of the P1s
*block* re-running — but the cost envelope check (`MO_REFACTOR_AUDIT_BUDGET_USD`)
will under-report by the `runs`-table delta until **G-03** ships. If you
intend to gate the next audit on a tighter envelope ($20, say), land **G-03**
first or be prepared for the breaker to fire late.

No other P1 blocks self-dispatch. The audit framework is dogfood-clean modulo
its own cost-accounting bug.

---

*End of synthesis. Findings cross-referenced to original lens reports at
`${MINI_ORK_RUN_DIR}/lens-{glm,kimi,codex,opus}.md`.*
