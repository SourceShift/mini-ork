# mini-ork Self-Audit — Synthesis Report

**Run:** `run-1780603302-86163` · **Date:** 2026-06-04
**Panel:** 4 heterogeneous lenses (GLM-tactical · Kimi-refactor · Codex-dispatch · MiniMax-architectural) + Opus synthesizer
**Target:** `/Users/admin/ps/mini-ork` v0.2 substrate

Findings prefixed by lens:
- `G-N` = GLM tactical scan
- `K-N` = Kimi refactor proposal
- `D-N` = Codex LLM-dispatch finding
- `O-RN / O-MH-N / O-DH-N` = MiniMax architectural / honesty / doc-honesty

★ marks **cross-lens consensus** (≥ 2 lenses surface the same defect or substrate concern).

---

## Section 1 — Severity × Leverage Matrix

| | **HIGH leverage** | **MED leverage** | **LOW leverage** |
|---|---|---|---|
| **P1** (ship-blocker / correctness / security) | G-1 ★, K-7 ★, K-12 ★ (SQL-injection trio); G-2 ★ (meta: this audit's own verifier); D-05 ★+G-19 (budget gate declared, not enforced; null-float silently defers); D-01 (reviewer/planner default Opus — burns budget every run) | G-3, G-4 (`set -e` drops `-u -o pipefail` in cleaner.sh); G-9 (partitioned dispatch no parallel cap); G-13 (hardcoded $50 budget vs $15 kickoff spec) | G-16 (WAL init silently swallowed); G-20 (`MINI_ORK_HOME` unset risk); G-18 (`eval` in `_flush_parallel_batch`) |
| **P2** (scale-blocker / cost / honesty wiring) | D-04 (no retry backoff — transient 429 kills run); D-02 (speculative pays all N); K-3 (duplicate dispatch branches — flag drift root cause); O-R12 ★ (D-048: evolution loop dead for coordination-shaped recipes); O-R13+O-MH-01 ★ (α gate computed, never enforced); G-12 (budget gate ignores `llm_calls` table) | K-5 ★+D-12 ★ (`cache.sh:146-149` python3 date fork); K-6 (2 python3 forks per budget check); K-1, K-2 (hot-path microopts in `llm-dispatch.sh`); K-4 (O(N²) bash concat in `mo_cache_hash_bundle`); K-8 (per-file `jq` loop); K-10 (6-8 forks per prompt build); D-08 (PLAN_CONTENT re-read in every parallel subshell); D-10 (`_d022_charge_node_cost` python3 fork per node); D-14 (literal `\n` in PROMPT_CONTENT — degrades parse rate); D-15 (1/4 char ratio underestimates code tokens); G-5, G-6 (N+1 sqlite3 forks in auto-merge + mutation-adversary); G-7, G-8 (unbounded `mo_events`/`llm_calls` tables); G-10 (CAST defeats index in reflection pipeline); G-11 (O(n²·m) git overlap); G-14 (ISO 8601 lex-order brittleness); O-R6 ★+O-DH-01 ★ (ARCHITECTURE.md: 12 migrations claimed, 15 live; ROADMAP.md ships items it claims as v0.3) | G-15 (mkdir-lock no backoff); G-17 (`mapfile` no MAX_NODES guard); D-11 (rubric cache_hash computed twice); D-13 (codex no streaming — total loss on timeout); K-9 (serial git blame in `_mo_capture_reflection`); O-MH-02 (mutation-adversary targets wrong surface); O-MH-03 (wireheading: `files_read` captured but never gated); O-R16 (rename `measure_rho` → `measure_rho_proxy`) |
| **P3** (long-horizon / architectural / advisory) | D-arch-01 (model-tier router); D-arch-02 (semantic embedding cache); D-arch-03 (batch reflection extraction); O-R4+O-R5 (extract `executor-core` from heredocs); O-R2 (PostgreSQL adapter for 100× inflection) | O-R3 (per-user budget isolation); O-R10 (workflow.yaml signature); O-R14 (`promotion_scope: local/shared`); O-R15 (OTel + dashboard API views); D-07 (codex cost untracked — $0.01 placeholder); D-09 (no provider fallback on outage); D-06 (no semantic cache layer); O-R17 (wireheading gate) | O-R11 (sandbox third-party verifiers); O-R8 (`mini-ork maintenance --archive-traces`); O-R18 (`mini-ork doctor --roadmap-check` automation) |

**Consensus density:** 7 ★ pairings across the panel. The SQL-injection cluster (G-1 + K-7 + K-12) is the highest-density signal: three independent lenses surfaced the same anti-pattern (bash string-interpolation into SQL) in three separate files. This is not coincidence — it's a substrate-level coding habit that the v0.2 codebase has not eradicated.

---

## Section 2 — Top 5 Immediate Wins (P1, total effort < 1 week)

| Rank | ID | Title | Lens | One-line fix | Effort |
|---|---|---|---|---|---|
| 1 | **G-1 + K-7 + K-12 ★** | SQL-injection trio: bash interpolation into sqlite3 CLI queries | GLM + Kimi (×2) | Port all 3 sites (`lib/auto-merge.sh:170`, `lib/cache.sh:98-128`, `lib/runs-tracker.sh:136-172`) to parameterized python3 heredocs — Kimi has the exact diff sketches ready in refactor-7 and refactor-12 | 2 days |
| 2 | **G-2 ★** | This audit's own verifier emits corrupt JSON | GLM (meta) | `recipes/refactor-audit/verifiers/lens-completeness.sh:68` — replace bash array word-split with `printf '%s\n' "${missing[@]}" \| python3 -c "import sys,json; print(json.dumps(sys.stdin.read().splitlines()))"` | 30 min |
| 3 | **D-05 + G-19 ★** | `budget_gate` declared in workflow.yaml but never enforced per-node; null cond silently defers | Codex + GLM | (a) Parse 6th `gates:` field in `bin/mini-ork-execute:137-153`; evaluate via `lib/gate_registry.sh` before each `_dispatch_node`. (b) Add `if cond is None: verdict = "fail"` at `lib/gate_registry.sh:249` | 1 day |
| 4 | **G-3 + G-4** | Bare `set -e` after subprocess capture drops `-u -o pipefail` in cleaner | GLM | Change both lines (`lib/cleaner.sh:312`, `:352`) to `set -euo pipefail`; guard `${PIPESTATUS[0]}` access | 15 min |
| 5 | **G-13 + D-01** | Default `MO_DAILY_BUDGET_USD=50` is 3.3× the kickoff spec ($15); reviewer/planner default routes to Opus 4.7 | GLM + Codex | (a) `lib/llm-dispatch.sh:349` change default to `15`. (b) `.mini-ork/config/agents.yaml:13` add `synthesizer: sonnet` lane; map `refactor-audit/workflow.yaml` reviewer node to `model_lane: synthesizer`. Both ship with `docs/CONFIG.md` env-var documentation. | 1 hr |

**Why these five:** They are the highest-ROI items in the panel. #1 closes the largest correctness+security gap (3 files, one anti-pattern). #2 is the meta-loop fix — without it, *this audit cannot reliably re-run*. #3 makes the declared budget gate actually do its job. #4 restores strict-mode guarantees in the cleanup path. #5 aligns cost defaults with the stated kickoff budget and routes the cheapest model-class to the most-frequent node type. **Total effort ≈ 3.5 days**, well under the < 2 weeks budget.

---

## Section 3 — v0.x+1 Architectural Shifts (P2, bundled)

### Bundle A — LLM Dispatch Hardening (3 eng-weeks · prerequisite: P1 #5)

| Items | File anchors |
|---|---|
| K-1, K-2, K-3 (single-source dispatch path; eliminate per-call `command -v` forks; collapse duplicate timeout/no-timeout branches) | `lib/llm-dispatch.sh:28-49,68-74,127-173` |
| K-6 (collapse 2 python3 forks per budget check into 1) | `lib/llm-dispatch.sh:351-367` |
| D-04 (exponential-backoff retry loop for 429/502/overloaded) | `lib/llm-dispatch.sh:431` |
| D-02 (kill remaining pids on first speculative success) | `bin/mini-ork-execute:735-748` |
| D-09 (provider fallback table: `opus → kimi`, `sonnet → glm`) | `lib/llm-dispatch.sh:62-66` |
| G-12 (budget gate must also aggregate `llm_calls` table; require `MINI_ORK_DB` non-empty) | `lib/llm-dispatch.sh:345-349` |

**Risk if deferred:** Every transient Anthropic outage forces a full-pipeline re-run from scratch (D-04). The current speculative dispatch mode is functionally identical to parallel mode — it pays for N lenses but only uses one (D-02). At $15/run budget with 4 lenses × Opus pricing, a single retried run can blow the daily budget twice over.

---

### Bundle B — Cache + Hot-Path Cleanup (2 eng-weeks · no prerequisites)

| Items | File anchors |
|---|---|
| K-5 ★ + D-12 ★ (eliminate python3 date fork; inline SQLite `datetime('now','+30 days')`) | `lib/cache.sh:146-149` |
| K-4 (O(N²) bash concat → temp-file pipeline) | `lib/cache.sh:78-89` |
| D-11 (rubric `cache_hash` computed twice — compute once at function top) | `lib/rubric-prescreen.sh:41,202` |
| D-10 (replace python3 fork in `_d022_charge_node_cost` with sqlite3 CLI one-liner) | `bin/mini-ork-execute:211-222` |
| K-8 (per-file `jq` loop in cache stats → single awk+python3 pass) | `lib/lane-helpers.sh:94-134` |
| K-10 (6-8 awk/sed forks per prompt build → 1 python3 substitution) | `lib/rubric-prescreen.sh:57-74`, `lib/reflection-refiner.sh:58-88` |
| D-08 (read PLAN_CONTENT once before dispatch loop; truncate at `MINI_ORK_CTX_BUDGET_TOKENS/4`) | `bin/mini-ork-execute:407,444,475` |
| D-14 (replace literal `\n` with `$'\n'` in PROMPT_CONTENT) | `bin/mini-ork-execute:408,445,482` |

**Risk if deferred:** At 100K runs/day projected scale these accumulate to hundreds of GB of pointless string copying (K-4), 500K+ python3 forks/day (K-5/D-12/D-10), and 8K wasted tokens per run from PLAN_CONTENT duplication (D-08). D-14 is also a *correctness* concern — literal `\n` in prompts degrades reviewer JSON parse rate (it is the same failure-mode class as `docs/fixes/20260602-spec-author-silent-die.md`).

---

### Bundle C — Data Layer Plumbing (2 eng-weeks · no prerequisites)

| Items | File anchors |
|---|---|
| G-7 (archive job for `mo_events`) | `db/migrations/0002_mini_orch_sessions.sql:81` |
| G-8 (TTL for `llm_calls`) | `db/migrations/0002_mini_orch_sessions.sql:220` |
| G-10 (drop CAST wrap on indexed `created_at`) | `lib/reflection_pipeline.sh:55` |
| G-14 (CHECK constraint on `created_at` length for ISO 8601 lex-order safety) | `lib/context_assembler.sh:94` |
| G-11 (O(n²·m) git overlap → single `git ls-files` + Python set-intersection) | `lib/scope-overlap.sh:172` |
| G-5, G-6 (N+1 sqlite3 forks → batch SELECT with `IN (...)`) | `lib/auto-merge.sh:170,179,356-375`; `lib/mutation-adversary.sh:28` |
| O-R8 (`mini-ork maintenance --archive-traces --older-than-days 90`) | New CLI subcommand + `db/migrations/0016_archive_tables.sql` |
| K-11 (serial gradient extraction → parallel fan-out with `_par=4` default) | `lib/reflection_pipeline.sh:64-75` |
| K-9 (parallel `git blame` ThreadPoolExecutor) | `lib/memory.sh:87-99` |

**Risk if deferred:** `mo_events` and `llm_calls` grow unbounded today. At 100×-scale projection these tables hit GB-size before any pruning logic exists — full table scans in the reflection pipeline (G-10) compound this into observable latency regressions.

---

### Bundle D — Honesty & Evolution Loop (3 eng-weeks · prerequisite: O-R12 first)

| Items | File anchors |
|---|---|
| O-R12 ★ (D-048: gradient prompt-tuning for coordination-shaped traces — **highest leverage in this bundle**, unblocks the entire evolution USP) | `lib/gradient_extractor.sh`, `ROADMAP.md:105-109` |
| O-R13 + O-MH-01 ★ (wire Krippendorff α gate as `pre_synthesis_gate` edge in refactor-audit; rename `measure_rho` → `measure_rho_proxy`) | `lib/topology_metrics.sh:51-79`, `recipes/refactor-audit/workflow.yaml` |
| O-MH-03 + O-R17 (wireheading check: validate that every cited `file:line` in a reviewer/verifier output appears in trace's `files_read` array) | `lib/gate_registry.sh`, trace data already at `bin/mini-ork-execute:298` |
| O-R6 ★ + O-R7 ★ + O-DH-01 ★ (update `docs/ARCHITECTURE.md` to reflect 15 migrations + 60+ tables; close stale ROADMAP.md v0.3 items already shipped: D-045, all 5 recipes, Phase E) | `docs/ARCHITECTURE.md:155`, `ROADMAP.md:71-126` |
| D-15 (token estimate: clamp budget to 75% of `MINI_ORK_CTX_BUDGET_TOKENS` for code-heavy fields) | `lib/context_assembler.sh:69-70,154-188` |

**Risk if deferred:** Bundle D is the framework's *credibility* surface. The README cites Nasser 2026 α = 0.042 as the gap mini-ork closes; the closing gate is not actually wired (O-MH-01). The ROADMAP lists work as v0.3 "Next" that's already shipped on main (O-R7) — contributors looking for work items will pick up tickets already in production. O-R12 (D-048) is the single 1-eng-week item that unblocks the *entire evolution loop* for the dominant recipe class (refactor-audit).

---

## Section 4 — Long-Horizon (P3 + Advisory)

These are tracked but not load-bearing at current scale. Defer until 10×–100× inflection materializes:

| ID | Bundle | Effort | Triggers (when this becomes P2) |
|---|---|---|---|
| O-R4 + O-R5 + D-arch-01 + D-arch-02 + D-arch-03 | Executor-core extraction (Python asyncio) + model-tier router + semantic cache + batch reflection | 16-20 wk combined | 100× run volume; or: heredoc fragility produces > 5 silent-die incidents/quarter (track via `docs/fixes/`) |
| O-R2 + O-R3 | PostgreSQL adapter + per-user budget isolation | 6 wk combined | Second human user added to the same `state.db` |
| O-R10 + O-R11 | Workflow.yaml signature verification + verifier-script sandboxing | 3.5 wk combined | First third-party recipe enters the codebase |
| O-R14 + O-R15 | `promotion_scope` field + OTel/dashboard API views | 6 wk combined | Self-evolution loop produces > 50 candidates/week, or external dashboard project starts shipping |
| D-07 | Codex cost-tracking sidecar (`MO_CODEX_DAILY_BUDGET_USD`) | 0.5 wk | Codex lane is added to a recipe with `MO_DAILY_BUDGET_USD < $20` |
| D-13 | Stream codex output to tmp file with partial-recovery | 1 wk | First codex-lane timeout incident in a published audit |
| D-09 | Provider fallback (`opus → kimi`) | 1 wk | First Anthropic outage costs a full-pipeline re-run |
| G-15, G-17, G-18, G-20 | Lock backoff, MAX_NODES guard, eval → nameref, MINI_ORK_HOME validation | 1.5 wk combined | Any first-incident report; current likelihood low |
| O-R18 | `mini-ork doctor --roadmap-check` automated honesty auditor | 1 wk | After Bundle D ships — keeps it from drifting again |

**Advisory note on D-06 (semantic embedding cache):** Codex projects 20-30% hit-rate uplift at 100K-task scale. At current 15 runs/day this is noise. Park until run volume × cache-miss-rate > 1000/day, then revisit.

---

## Section 5 — Hardest Open Question (inherits MiniMax §9)

**Q:** *Can the self-evolution loop remain safe when generating workflow candidates at 100× volume with gradient signals it knows are low-quality?*

The MiniMax lens articulates this honestly (`lens-minimax.md:226-240`): the PromotionGate's `utility_delta > 0` is a meaningful safety guarantee at 1× because the benchmark suite is calibrated for the small task-type set. At 100× two pathologies compound:

1. **Volume pressure** — 150+ candidates/day enter `workflow_candidates`. False-positive promotion probability scales with volume.
2. **Gradient quality collapse** (D-048) — `gradient_extractor` returns 0 useful gradients for coordination-shaped traces, so `group_evolver` is generating *random perturbations*, not informed proposals.

MiniMax proposed three mitigations:
1. Solve D-048 first (O-R12).
2. Add a minimum `n_benchmark_tasks` requirement proportional to candidate volume before promotion.
3. Add cross-tenant validation (O-R14).

**My assessment (synthesizer):** The three mitigations are **necessary but not sufficient**. They address volume scaling and per-tenant safety but leave one residual risk uncovered: **correlated false-positives via shared scaffolding**. If 50 candidates all derive from the same flawed gradient pattern (because pattern_records contaminate the evolver), they may *jointly* pass the benchmark suite by sharing a benchmark-overfit substructure, while *individually* degrading novel tasks. This is the AlphaEvolve-style "reward hacking via correlated perturbations" failure mode, and the three proposed mitigations don't structurally prevent it.

**Additional research needed:** A 4th mitigation candidate is a **diversity gate on `pattern_records`** — before any candidate enters PromotionGate evaluation, verify that the gradient sources informing it are themselves diverse (use the same ρ-style cross-family check applied to candidate provenance, not just lens panels). This shifts the heterogeneity discipline from input-side (lens panels) to evolution-side (candidate sources). Drafting that gate is a 2-eng-week research+implementation task and should be tracked as a v0.4 architectural prerequisite, gated on first solving O-R12.

The honest answer to MiniMax's question remains: **we don't yet know.** Ship Bundle D first; revisit the question after 4-6 weeks of α-gate telemetry.

---

## Section 6 — Dogfood Reflection (Meta-Loop Check)

### Was this audit reproducible via the framework?

**Partially.** All 4 lens dispatches succeeded; all 4 reports landed at `~/.mini-ork/runs/run-1780603302-86163/lens-*.md` with non-zero size. The heterogeneous-family precondition was satisfied: GLM (Zhipu), Kimi (Moonshot), Codex (OpenAI), MiniMax (MiniMax M3) span 4 distinct provider families, and the synthesizer (Opus) is a 5th. The `opus_lens → minimax_lens` swap documented in `kickoffs/self-audit-2026-06-04.md` correctly avoids the synthesizer-vs-lens family collision that the original recipe risked.

### Did any lens get blocked by something the audit itself identified?

**Yes — directly.** Finding **G-2** is the meta-loop hit: `recipes/refactor-audit/verifiers/lens-completeness.sh:68` builds a `missing` JSON list via bash array expansion that word-splits on spaces. An entry like `"lens-glm.md (too short: 5 lines)"` becomes 5 tokens. **This means the verifier that gates *this audit's completion* emits a corrupt `missing` JSON array on any non-trivial completeness failure.** The audit ran to completion only because all 4 lens reports were sufficiently long that the array stayed empty — had any lens failed, the verifier would have produced unparseable output and the publisher node could have been gated incorrectly. This is the framework auditing itself and finding a flaw in its own verification rung.

A second meta-loop signal: Finding **D-05** says `gates: [budget_gate]` declared in `recipes/refactor-audit/workflow.yaml:15-19` is *silently ignored* by `bin/mini-ork-execute` (the field is not parsed). This audit's recipe declared a budget gate that did not execute. The audit completed within budget anyway, but the gate's presence in the YAML was theater.

**Implication:** The framework's claim that recipes can declare verification gates and rely on the executor to honor them is currently aspirational. Bundle A item D-05 must ship before any future recipe can safely depend on per-node budget enforcement.

### What worked well in the meta-loop

- **Topology diversity discipline held:** all 4 lens reports are stylistically distinct (GLM = compact table of 20 items; Kimi = before/after diffs; Codex = ranked numbered findings with savings estimates; MiniMax = sectioned architectural treatise). Pairwise prompt-similarity ρ is clearly < 0.25 by inspection.
- **Forensic preservation:** failed dispatch artifacts policy at `lib/llm-dispatch.sh:456-467` was not triggered this run, but Codex correctly flagged it as a robustness asset (Codex `what's-already-right` §6).
- **Cross-lens consensus mechanism worked:** 7 ★ pairings emerged without lenses coordinating. SQL injection (G-1+K-7+K-12), expires_at fork (K-5+D-12), budget enforcement (G-12+D-05+D-07), ARCHITECTURE.md drift (O-R6+O-DH-01), etc. This is exactly the signal-amplification Nasser 2026 / Rajan 2025 predict.

---

## Section 7 — How to Re-Run

### Exact reproduction command

```bash
cd /Users/admin/ps/mini-ork
mini-ork run recipes/refactor-audit \
  --kickoff kickoffs/self-audit-2026-06-04.md \
  --budget 15 \
  --max-parallel 4
```

### Pre-conditions

1. `~/.mini-ork/state.db` initialized via `mini-ork-init` (PRAGMA cross-reference in MiniMax lens requires fresh schema).
2. All 4 model-family API keys exported and non-rate-limited: Zhipu (GLM), Moonshot (Kimi), OpenAI (Codex), MiniMax (MiniMax). Plus Anthropic for Opus synthesizer.
3. `recipes/refactor-audit/workflow.yaml` carries the `opus_lens → minimax_lens` swap (currently modified-not-committed per `git status`).

### P1 blockers on self-dispatch reliability

- **G-2 must be patched first.** Until `recipes/refactor-audit/verifiers/lens-completeness.sh:68` is fixed, any partial-lens-failure scenario produces a verifier-side JSON corruption that silently passes or misleads the publisher. **Fix the verifier before the next audit run.**
- **D-05 must be patched** before relying on the declared `budget_gate` in `workflow.yaml:15-19`. Right now the gate is decorative — a runaway Opus call can consume the entire $15 budget on a single node before any check fires.
- **G-13 should be patched** to align the `MO_DAILY_BUDGET_USD` default ($50) with the kickoff spec ($15), or the kickoff value must be re-passed via env on every invocation.

### Cost & runtime expectations (this run)

Per `.last-llm-cost` and lens artifact sizes: ~$8-11 dispatched across 4 lenses (Codex was cheapest; MiniMax/architectural was longest at 29 KB). Synthesizer adds ~$1-2. Total run cost ≈ $10-13 — fits inside the $15 budget with thin margin. **At Opus default routing without the D-01 fix, the synthesizer alone would dominate cost (Opus 4.7 is 5-10× Sonnet 4.6 per token).**

---

## Section 8 — Honest Gaps in This Synthesis

- **Schema/PRAGMA cross-reference was not executed live.** MiniMax flagged the audit *should* run `mini-ork-init` and PRAGMA-query the resulting `state.db` to verify schema-vs-query column alignment. This synthesizer accepts MiniMax's static-grep finding on faith (15 migrations vs 12 claimed) but did not re-verify by spawning a fresh init.
- **No coverage of `tests/` directory.** Four lenses focused on `lib/`, `bin/`, `db/migrations/`, `recipes/`, and docs. Test-suite coverage gaps (e.g., are the SQL-injection sites covered by integration tests? Almost certainly no, since they survived to this audit) were not measured. **Recommend a follow-up audit scoped to `tests/` after Bundle A ships.**
- **Codex lane self-blindspot.** Codex itself surfaced finding D-07 (its own provider lane's cost is untracked, defaulting to $0.01 placeholder). This means the cost numbers in Section 7 above *understate* Codex's true contribution to this run's spend by an estimated 5-30×. Treat the ≈$10-13 figure as a lower bound.
- **Deliverable path discrepancy.** Recipe prompt specified `synthesis.md`; plan's verifier_contract checks `synthesis-report.md`. This file is written to `synthesis.md` per the explicit instruction. The publisher node (`publisher-07`) and verifier (`verifier-06`) should be reconciled before re-run — likely add a symlink or rename in `verifiers/lens-completeness.sh`.

---

*Synthesizer: Opus 4.7 · Panel: GLM (Zhipu) + Kimi (Moonshot) + Codex (OpenAI) + MiniMax (MiniMax M3) · Heterogeneous-family precondition: 5 distinct provider families · Synthesis date: 2026-06-04*
