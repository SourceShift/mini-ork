# mini-ork 4-Lens Audit — Synthesis

**Run:** `run-1780604422-58608`
**Date:** 2026-06-04
**Panel:** GLM (tactical) · Kimi (refactor) · Codex (LLM dispatch) · MiniMax (architecture, Opus-slot)
**Synthesizer:** Anthropic Opus
**Lens reports:** `lens-glm.md`, `lens-kimi.md`, `lens-codex.md`, `lens-minimax.md`

Findings prefixed `G-N` (GLM), `K-N` (Kimi), `D-N` (Codex), `O-RN` (MiniMax/Opus-slot).
★ = surfaced by ≥2 lenses (consensus signal per Rajan 2025).

---

## Section 1 — Severity × Leverage Matrix

|  | **HIGH leverage**<br/>(framework-wide, multi-recipe blast) | **MED leverage**<br/>(single subsystem) | **LOW leverage**<br/>(local cleanup) |
|---|---|---|---|
| **P1**<br/>blocks-NOW | **G-1** trace `prompt_version_hash` column drift (destroys all reflection lineage)<br/>**G-2 ★ + D-4 ★ + O-R18 ★** budget enforcement triple-gap (wrong default, 5 bypass callsites, no preflight)<br/>**G-4 ★ + D-5 ★** Codex phantom $0.01 + cost invisible to circuit breaker<br/>**G-5 ★ + O-R14 ★ + O-R15 ★** Rajan 2025 ρ gate never enforced (observed only) | **K-6** literal `\n` in researcher/implementer/reviewer prompts (line 408, 444, 485)<br/>**G-3** budget timestamp INTEGER vs ISO-string mismatch<br/>**D-3** `speculative` dispatch never cancels siblings (= `parallel` + cost) | **G-1** trace col drift (1-line fix; high leverage placed above) |
| **P2**<br/>blocks-1K-runs | **K-2 ★ + K-3 ★ + K-9 ★ + G-20 ★ + G-21 ★** SQL-injection sweep (`cache.sh`, `memory.sh`, `auto-merge.sh`, `runs-tracker.sh`)<br/>**K-1 ★ + D-2 ★ + D-arch-2** reflection pipeline serial-per-trace (4–8h wall time → batch or xargs-P)<br/>**O-R7** persistent `mini-ork-db-server` shim (kills 80ms × N python3 forks)<br/>**D-arch-1** complexity-tier router (30–40% input-token cost cut)<br/>**O-R16** gateway-model cost normalization (MiniMax/GLM/Kimi report $0 today) | **G-7** `auto-merge.sh` 5–7× sqlite3 N+1 per epic<br/>**G-8 + G-9** scope-overlap O(N·P) + O(N²) pairwise<br/>**G-10** `CAST(strftime(...))` defeats `idx_execution_traces_created_at`<br/>**G-11** per-node `_d022_charge_node_cost` python3 fork<br/>**G-12** no exponential backoff on 429/5xx (`mo_llm_dispatch`)<br/>**G-17** `cleaner.sh` lock has no timeout-retry (unlike `auto-merge.sh`)<br/>**D-9** `fallback_below` declared in agents.yaml but never consulted<br/>**O-R1 + O-R2** `busy_timeout` 5s→30s and `MAX_PARALLEL` 4→8 for audit recipes | **K-4** dispatch timeout-branch dedup (20 LOC dup)<br/>**K-12** redundant `source` in `gradient_extract` (1000 file-reads/run)<br/>**K-13** find+xargs+ls -t plan.json lookup |
| **P3**<br/>blocks-100K + bite-later | **O-R3** PostgreSQL backend (move from v1.0 → v0.4)<br/>**O-R4** `mini-ork-worker` daemon (horizontal dispatch)<br/>**O-R5** `mini-ork-control-plane` HTTP service (multi-tenant)<br/>**O-R6** secrets → Vault / AWS SM<br/>**O-R11** recipe semver manifest + `mini-ork recipe validate`<br/>**O-R12** verifier sandboxing (`firejail` / `sandbox-exec`)<br/>**O-R13** SHA-256 hash check for `cl_*.sh` before `source`<br/>**G-13 ★ + O-R9 ★** `execution_traces` / `model_costs` TTL + archive ladder | **K-7** rubric awk tmpfile chain → python3 single-pass<br/>**K-8** O(N²) jq-in-loop for cache stats<br/>**K-11** unstructured pipe-concat rationale → JSON array<br/>**D-6** flat-sleep retry → exponential backoff + jitter<br/>**D-8** input normalization before cache hashing (5–15% extra hits)<br/>**D-10** `_MO_LANE_*` env-var cache misses on parallel cold fork<br/>**D-11** Codex executable lane forced to `text` (skips `.cost` sidecar)<br/>**D-12** duplicate `cache_hash` in rubric-prescreen<br/>**D-arch-3** semantic dedup cache layer (cosine ≥0.97)<br/>**O-R8** partition `execution_traces` / `model_costs` / `gradient_records` by ts<br/>**O-R10** Flyway/sqitch for migration management<br/>**O-R17** per-tenant OTel span export | **G-14** `for d in $(ls -d …)` word-splitting (paths with spaces)<br/>**G-15** `mapfile -t NODE_IDS` with no cap (10K-node plan loads entire array)<br/>**G-18** `docs/ARCHITECTURE.md:155` stale ("12 migrations, ~45 tables" → 15/~60)<br/>**G-22** `mo-steer.sh` 30s linear poll<br/>**G-23** topology errors silently swallowed via `\|\| echo ""`<br/>**G-24** `eval` in `_flush_parallel_batch`<br/>**G-25 ★** ROADMAP missing `Status: not-started` markers (also positioning-doc gap) |

**Consensus density:** 11 findings have ★ marks. The five strongest consensus clusters are (a) **budget enforcement**, (b) **Codex phantom cost**, (c) **Rajan 2025 ρ gate**, (d) **SQL injection across cache+memory+auto-merge+runs-tracker**, (e) **reflection pipeline serial-LLM-per-trace**.

---

## Section 2 — Top 5 Immediate Wins (P1)

Ranked by **(severity × leverage) ÷ effort**. Effort sums to **~3.5 dev-days** — well under the 2-week cap.

| # | ID | Title | Source lens | One-line fix | Effort |
|---|---|---|---|---|---|
| 1 | **G-1** | `trace_write` writes empty string to `prompt_version_hash` (silent column-name drift) | GLM | In `lib/trace_store.sh:69`, change `p.get("prompt_version", "")` → `p.get("prompt_version_hash", "")` and verify all callers pass the `_hash`-suffixed key | 1h |
| 2 | **G-4 + D-5 ★** | Codex phantom $0.01 inflates ledger AND real Codex spend invisible to circuit breaker | GLM + Codex (consensus) | `lib/providers/cl_codex.sh:101` — emit real cost from `codex exec` `tokens used:` stderr at `$0.0025/1K`; in `bin/mini-ork-execute:202` fall back to `0`, not `0.01` | 0.5d |
| 3 | **G-2 + D-4 ★ + O-R18 ★** | Budget triple-gap: default $50 (spec is $15), 5 callers bypass `llm_dispatch`, no preflight gate | GLM + Codex + MiniMax (3-lens consensus) | (a) `lib/llm-dispatch.sh:349` default → 15; (b) route `reflection-refiner.sh:115`, `rubric-prescreen.sh:111`, `mutation-adversary.sh:114`, `cleaner.sh:299`, `_worker-launcher.sh:247` through `mo_llm_dispatch`; (c) add preflight `[ "$(mini-ork spent-today)" -lt "$MO_DAILY_BUDGET_USD" ]` at `bin/mini-ork-execute` start | 1d |
| 4 | **G-5 + O-R14 + O-R15 ★** | Rajan 2025 ρ precondition is observed (`panel_topology_telemetry`) but never gated — silent coalition runs pass | GLM + MiniMax (consensus) | In `bin/mini-ork-execute:632` after `measure_topology`, if `rho >= 0.25` then write run verdict `COALITION_ABORT` and refuse to invoke synthesizer. Also wire `family_diversity_gate` health probe at recipe start | 1d |
| 5 | **K-6** | Literal `\n` in double-quoted prompt assembly — LLMs see escape sequences, not blank lines | Kimi | `bin/mini-ork-execute:408,444,485` — replace `"…\n\n…"` with `"…"$'\n\n'"…"` across all three node-type prompt blocks | 1h |

**Why these five:** All five are evidence-anchored to a single `file:line`, three are multi-lens consensus, and each closes a *correctness* (not optimization) gap. Items 2–4 together make the daily budget enforcement actually load-bearing — without all three, the documented $15/day cap is fiction.

---

## Section 3 — v0.x+1 Architectural Shifts (P2)

Bundled by theme. Total ≈ **9–12 eng-weeks** if all four bundles execute; each bundle is independently shippable.

### Bundle A — Cost & Dispatch Hardening (2–3 wk)

| Item | Source | Notes |
|---|---|---|
| K-1 + D-2 + D-arch-2 ★ | Kimi + Codex | Reflection batch-mode: collapse 500 serial Sonnet calls into 1–3 multi-trace batch calls (~90% cost cut + 200× latency cut) |
| G-12 + D-6 | GLM + Codex | Exponential backoff with jitter inside `mo_llm_dispatch` (replaces post-hoc healer detection) |
| D-9 | Codex | Wire `fallback_below` from `agents.yaml` → actual retry path |
| D-arch-1 | Codex | Complexity-tier router (route by prompt-token count, not by node_type) |
| O-R16 | MiniMax | Per-provider cost extractor for MiniMax/GLM/Kimi gateways (today they report $0) |

**Prerequisite P1s:** Items 2 + 3 from §2 (Codex cost + budget gate). Risk if deferred: daily-budget gate continues to under-count real spend by 50–75% on heterogeneous recipes (`O-R16`).
**Total effort:** 2–3 wk.

### Bundle B — SQL Safety + Throughput Sweep (1.5–2 wk)

| Item | Source | Notes |
|---|---|---|
| K-2 + K-3 + K-9 + G-20 + G-21 ★ | Kimi + GLM (5-finding consensus) | Migrate `cache.sh:101–163`, `memory.sh:193–532`, `auto-merge.sh:363`, `runs-tracker.sh:137–173` from string-interp SQL to parameterized python3 (pattern already used in `trace_store.sh`) |
| G-7 | GLM | `auto-merge.sh` 5–7× sqlite3 N+1 → single heredoc per epic |
| G-8 + G-9 | GLM | `scope-overlap.sh` O(N·P) git-in-loop + O(N²) pairwise → file→epic reverse-map |
| G-10 | GLM | Drop `CAST(strftime(...))` in `reflection_pipeline.sh:55` to restore index usage |
| K-1 + G-11 | Kimi + GLM | Per-node `_d022_charge_node_cost` python3 fork → batch at run finalization |

**Prerequisite P1s:** None. Risk if deferred: any kickoff title containing a `'` silently corrupts the cache row; under adversarial input this is a real injection vector.
**Total effort:** 1.5–2 wk.

### Bundle C — Runtime Substrate (3–5 wk)

| Item | Source | Notes |
|---|---|---|
| O-R7 | MiniMax | Persistent `mini-ork-db-server` shim — eliminates 80ms × N python3-startup tax per run |
| O-R1 + O-R2 | MiniMax | `busy_timeout` 5s→30s at every `sqlite3.connect()`; `MINI_ORK_MAX_PARALLEL` default 4→8 for audit recipes |
| G-13 + O-R9 ★ | GLM + MiniMax (consensus) | `mini-ork maintenance --archive-traces --older-than 90d` (named in ROADMAP as O-R8 but not built); TTL ladder: hot 90d → cold archive → 2y delete (audit_log immutable) |
| O-R8 | MiniMax | Partition `execution_traces` / `model_costs` / `gradient_records` by `(run_id, ts)` |
| O-R3 | MiniMax | PostgreSQL backend (promote from v1.0 → v0.4 per recommendation) |

**Prerequisite P1s:** None. Risk if deferred: at 100K dispatches/day `execution_traces` reaches 36.5M rows/year with no rotation; SQLite WAL writer becomes the ceiling at ~1K runs/day shared-team deployment.
**Total effort:** 3–5 wk (skip Postgres for a 1-wk shim-only path).

### Bundle D — Heterogeneity Enforcement (2–3 wk)

| Item | Source | Notes |
|---|---|---|
| O-R14 ★ | MiniMax | `family_diversity_gate`: pre-flight provider-family health probe; abort if any required family is down |
| O-R15 ★ | MiniMax | `krippendorff_alpha_gate`: per-run α across first-round lens proposals (Nasser 2026); auto-escalate to `human_gate` below 0.4 |
| O-R13 | MiniMax | SHA-256 checksum for `lib/providers/cl_*.sh` before `source` (closes supply-chain gap pre-marketplace) |
| G-19 | GLM | `_mo_llm_is_gateway` becomes a registered list; add test asserting every `cl_*.sh` is `is_executable` OR `is_gateway` |

**Prerequisite P1s:** Item 4 from §2 (ρ gate). Risk if deferred: the framework's whole USP — Rajan 2025 submodularity precondition — remains documentation-only.
**Total effort:** 2–3 wk.

---

## Section 4 — Long-horizon (P3 + advisory)

Tracked but not load-bearing in current single-host, single-operator deployments:

- **O-R4** — `mini-ork-worker` daemon (horizontal node dispatch). Required at 100×; not before.
- **O-R5** — `mini-ork-control-plane` HTTP service (multi-tenant scheduling + per-tenant budgets). 12–16 wk; only needed at 1000× / hosted SaaS.
- **O-R6** — Vault / AWS Secrets Manager replacement for `secrets.local.sh`. Wait until O-R5 lands.
- **O-R11 + O-R12** — Recipe semver + verifier sandboxing (`firejail` / `sandbox-exec`). Required *before* opening a third-party recipe marketplace; not before.
- **O-R10** — Flyway/sqitch migration tool. Current 50-LOC runner works through 15 migrations; revisit at 30+.
- **O-R17** — OTel span export. Defer until `mini-ork metrics` becomes insufficient (no signal yet).
- **D-arch-3** — Semantic dedup cache layer (cosine ≥0.97). Estimated 15–25% additional hit rate; defer until exact-hash cache + input normalization (`D-8`) land first.
- **G-18 + G-25 ★** — Doc honesty drift (`ARCHITECTURE.md:155` stale counts; ROADMAP missing `Status: not-started` markers). Single PR can fix both.

**Advisory only (no action recommended):** G-22 (`mo-steer.sh` polling), G-23 (topology silent-drop), G-24 (`eval` in batch-flush). These are stylistic, low-blast, and the codebase has more important debt.

---

## Section 5 — Hardest Open Question (inherited from MiniMax §7)

**How does the self-evolution loop avoid retraining on its own hallucinations?**

The benchmark suite's promotion gate for `research_synthesis` and `refactor-audit` task classes is LLM-judged by the same family distribution that produces the candidates (`lib/benchmark_suite.sh`, `lib/promotion_gate.sh`). If all four families share a systematic blind spot, the promotion gate cannot detect it — it is measuring **consensus of a coalition**, not external ground truth. This is Zietsman 2026's circularity gap applied to the *evolution loop itself*, not just the per-run audit.

MiniMax §7 sketches three partial mitigations:

| Mitigation | Adequacy assessment |
|---|---|
| 1. **Automated citation tracing** — does each finding cite a `file:line` that exists and contains the alleged pattern? | **Partial.** Catches fabrication (a finding pointing to a non-existent line) but does not catch *misinterpretation* of code that does exist. False-negative on systematic blind spots is unchanged. |
| 2. **Coverage of injected bugs** (Agarwal 2026 fabricated-bug injection) | **Necessary but not sufficient.** Verifies the auditor recall against a known-bug oracle, but the injected-bug set is itself authored by humans whose blind spots may correlate with the LLM panel's. The oracle is only as honest as its author. |
| 3. **Krippendorff α across validator families** (Nasser 2026) | **Strongest of the three, but assumption-loaded.** α-disagreement signals diversity; α ≥ 0.8 may mean "all four families share the same blind spot" rather than "all four families are correctly converging." Without an external ground-truth anchor, α distinguishes *agreement* from *correctness* only probabilistically. |

**Verdict:** The three mitigations together raise the bar but do not close it. The honest state is that for `code_fix` and `db_migration` task classes the benchmark oracle *is* deterministic (typecheck + targeted test), and the evolution loop is grounded there. For `research_synthesis` and `refactor-audit`, no deterministic oracle exists yet.

**Research need (P3, not P1):** Either (a) build a **deterministic citation+coverage oracle** for synthesis recipes (cite a real `file:line` AND contain ≥X% of known-injected bug shapes), accepting that this is a recall-only oracle and precision remains LLM-judged, OR (b) accept structurally that synthesis-class recipes evolve more slowly — manually promoted, not auto-promoted — until a non-LLM oracle is found. Today's framework leans implicitly toward (b) but the ROADMAP wording does not state this explicitly. **Recommended honesty patch:** Add a one-paragraph note to `docs/positioning/why-mini-ork.md:152` stating that auto-promotion is restricted to task classes with deterministic oracles, and synthesis-class candidates require operator review.

---

## Section 6 — Dogfood Reflection

**Was this audit reproducible via the framework?** Yes. The `refactor-audit` recipe dispatched the 4-lens panel via `mini-ork-execute`, all four lens outputs landed at `~/.mini-ork/runs/run-1780604422-58608/lens-*.md`, and the synthesizer (this document) is the dispatched `synthesizer` node per the recipe DAG.

**Did any lens get blocked by something the audit itself identified?** Yes — three meta-loop hits:

1. **G-4 + D-5 ★ (Codex phantom cost):** The Codex lens consumed real OpenAI Codex API tokens. The dispatched run charged `$0.01` flat to `task_runs.cost_usd`. The audit's own daily-budget telemetry under-counts the real spend it just incurred. The audit is honest about the bug it triggered.

2. **G-5 + O-R14 (ρ gate not enforced):** The 4-lens panel ran without a pre-flight check that all four families were online and that pairwise output similarity stayed below the Rajan 2025 coalition threshold. If, hypothetically, GLM and Kimi had returned highly correlated outputs (ρ ≈ 1.0), this synthesis would still have been produced — silently degrading the panel's evidence value below its claimed precondition. The audit surfaced the gap; the audit itself ran outside the gate.

3. **G-12 (no exponential backoff):** If any lens had hit a 429, the healer would have applied a flat 30s sleep. No lens hit a 429 this run (the `.last-llm-cost` ledger shows clean dispatches), but the framework would not have recovered gracefully under sustained rate-limit pressure. The audit identified the gap before it bit.

**Net:** The framework can audit itself, and the audit's first findings are reasons the audit's own dispatch was less safe than its documentation claims. This is the healthiest possible meta-loop outcome — better than a clean run, because it produces a falsifiable to-do list.

---

## Section 7 — How to Re-run

**Bare command (current state, unsafe re-dispatch under strict $15 cap):**

```bash
mini-ork-execute \
  --recipe refactor-audit \
  --kickoff kickoffs/self-audit-2026-06-04.md
```

**Safe re-dispatch (recommended — apply §2 P1 items first):**

1. Apply P1 items 1–4 from §2 (≤3 dev-days).
2. Verify `MO_DAILY_BUDGET_USD=15` is honored end-to-end:
   ```bash
   mini-ork spent-today  # must reflect real spend including Codex
   ```
3. Re-run with explicit ρ gate enforcement:
   ```bash
   MO_RHO_THRESHOLD=0.25 MO_FAMILY_DIVERSITY_GATE=strict \
     mini-ork-execute --recipe refactor-audit \
     --kickoff kickoffs/self-audit-2026-06-04.md
   ```

**P1 that blocks safe self-dispatch:** **§2 item 2 (G-4 + D-5 ★ Codex cost)**. Until Codex cost is correctly attributed, the daily-budget circuit-breaker cannot stop a runaway 4-lens panel from spending past the documented $15 cap. This is the single most important fix before re-running this audit at any scale beyond the single-shot diagnostic above.

---

*Synthesis composed by Anthropic Opus from glm + kimi + codex + minimax lens reports. All ★ marks denote multi-lens consensus per Rajan 2025 panel-method evidence weighting. File:line citations preserved verbatim from source lens reports and not independently re-verified by the synthesizer.*
