---
title: GEPA ↔ Memory ↔ Context-Engine Integration & Correctness Audit — Synthesis
feature: prompt_evolution
doc_type: research
status: active
version: 1.0
last_updated: 2026-06-25
last_verified: 2026-06-25
owner: "@mini-ork-synthesizer"
audience: agent+human
supersedes: null
replaced_by: null
canonical_path: docs/_meta/research/20260625-gepa-memory-contextengine-integration-audit.md
tags: [gepa, memory, context-engine, integration-audit, zero-fallback, chart-drift, prompt-harness]
---

# GEPA ↔ Memory ↔ Context-Engine — 4-Lens Integration Audit (Synthesis)

**Run:** `run-1782379484-31281` · **Lenses:** GLM/MiniMax (tactical breadth), Kimi (code-level correctness), Codex (LLM-dispatch/cost + wiring), Opus (architectural shape) · **Mode:** read-only.

**Finding IDs:** `G-N` = GLM, `K-N` = Kimi (refactor-N), `D-N` = Codex (finding-N), `O-RN` = Opus recommendation N. **★** = consensus (surfaced by 2+ lenses).

**The one-paragraph verdict.** Of the three subsystems only **GEPA→Harness (Seam A) is wired end-to-end**. Memory↔Context-Engine (Seam B) and Compose→Chapter-Writing (Seam C) are *dark* — built, imported, but no data flows through them (Opus §1-4). On top of that structural gap sits a dense cluster of **fail-open Zero-Fallback violations** (Kimi K-3..K-7, K-15) and **chart-drift** that silently no-ops memory hardening and two crons in prod (G-3, G-4, O-R8, O-R10). The cheapest, highest-consequence wins are config-surface + fail-closed flips; the substrate work is closing Seam B by making memory a budgeted compose layer.

---

## Section 1 — Severity × Leverage Matrix

Cells list finding IDs. **★** marks consensus (2+ lenses on the same defect).

| | **HIGH leverage** | **MED leverage** | **LOW leverage** |
|---|---|---|---|
| **P1** (ship ≤2 wk) | **G-3★/O-R8★** (HARD_FAIL fail-open), **K-1★** (Mem0 app read-after-write break), **G-12** (compose cache cross-user leak), **K-4** (ACL single-row fail-open), **K-7** (write-gate wrong user_uuid), **K-5** (calibrator Redis fail-open), **G-1/G-10** (redteam_quarterly silent no-op) | **K-2** (skill provenance `'system'`), **K-6** (bucket-perm fail-open), **K-13** (cache TTL NaN → write 500), **K-3★/D-9** (compose layer silent-empty), **K-15** (hebbian silent swallow) | **K-14** (kalman clamp), **K-8** (driftProbe deleted-block filter), **G-4** (LLM_JUDGE_CRON missing), **G-8** (adaptive-attack cron outside gate) |
| **P2** (v0.x+1) | **G-5★/G-6★/K-9★/K-10★/D-6★/O-Seam-B** (GEPA harness bypass), **O-R2★/O-R3★/G-11** (Mem0→compose layer + score_metadata), **K-11** (shadow experiment never scheduled) | **D-arch2** (batch structured API), **D-arch1** (budget envelope), **O-R5** (wire one chapter-compose, delete dup), **O-R6** (contract hot read-path), **D-1/D-2/D-8/D-10/D-12/D-15** (serial dispatch) | **D-arch3** (gateway cache policy), **D-13** (compose-hint before deterministic route), **D-5** (judge tier escalation always) |
| **P3** (tracked) | **O-R4** (system-of-record decision), **G-7★/O-R11★** (consolidate memory flags off SealedSecret) | **G-9** (dead barrel), **G-18/G-19/G-22** (dead exports), **D-14** (multimodal overlap) | **G-14/G-21** (naming collisions), **G-20** (file placement), **G-2** (dual red-team path), **O-R9★** (BCE_M3 dual home) |

**Consensus signal map** (the defects ≥2 lenses independently flagged — fix these first, the cross-validation is your confidence):

- **`FEATURE_MEMORY_HARD_FAIL` fail-open** — G-3, Kimi chart table (`lens-kimi.md:538`), O-R8. **3 lenses.**
- **GEPA bypasses the prompt harness** — G-5/G-6, K-9/K-10, D-6, Opus Seam B. **4 lenses.**
- **Memory flags absent from `values.yaml` (chart-drift)** — G-7, Kimi chart table, Codex "what's already right" (`lens-codex.md:112`), O-R11. **4 lenses.**
- **Compose layer fail-open / Zero-Fallback** — K-3, D-9 (different sites, same anti-pattern). **2 lenses.**
- **Mem0 read-after-write / Seam-B silo** — K-1 (the `app` mismatch), Opus §3. **2 lenses.**
- **`BCE_M3_CALIBRATOR` dual config home** — GLM flag matrix (`lens-glm.md:56`), O-R9. **2 lenses.**

---

## Section 2 — Top 5 Immediate Wins (P1)

Ranked by ROI = (severity × leverage) ÷ effort. Total effort < 2 weeks.

| Rank | ID | Title | Lens | One-line fix | Effort |
|---|---|---|---|---|---|
| 1 | **K-1★** | Mem0 `app` mismatch makes every trajectory write invisible to read | kimi | Write `app: BOOK_GEN_MEMORY_APP` + move `trajectory_kind` into `metadata` (`bookMemoryService.ts:294`/`:373`); dual-read window before dropping legacy filter | 0.5d |
| 2 | **G-12** | Compose cache leaks progressive-delivery `delivered_uuids` across users | glm | Add `user_uuid` + `session_uuid` to `hashQuery` key composition in `contextEngine/cache.ts` | 0.5d |
| 3 | **G-3★ / O-R8★** | `FEATURE_MEMORY_HARD_FAIL` absent in all 5 config surfaces → CAM silently OFF in prod | glm+opus | Mirror flag to `values.yaml` backend.env + `server/.env.example` (explicit `'false'`); add `logger.info` on flag-OFF at `memoryHealthProbe.ts:35`; **decide** book-gen posture (flip on or document best-effort) | 0.5d |
| 4 | **K-4 / K-7** | ACL single-row fail-open + write-gate fed a block UUID as `user_uuid` | kimi | `acl.ts:122` return `{allowed:false}` on zero rows; `operations.ts:329` resolve real owner `user_uuid` via `blocks` lookup (reuse the K-6 lookup) | 1d |
| 5 | **G-1 / G-10** | `redteam_quarterly` enqueued but not in `PROMPT_EVOLUTION_PIPELINE_TYPES` → fires into a silent `{success:true, itemsProcessed:0}` no-op every quarter | glm | Add `'redteam_quarterly'` to the type list (`promptEvolutionCronDispatch.ts:33`) + a `case` in `_dispatch` (`:96`) calling `handleRedTeamDrill` (`redTeamCron.ts:309`) | 1d |

**Runner-up P1s** (same sprint, lower individual ROI but trivial): K-5 (calibrator `return POSITIVE_INFINITY` on Redis-down, `calibrationGate.ts:141`), K-13 (validate `CONTEXT_CACHE_TTL_SECONDS`, `cache.ts:223` — currently `NaN` 500s every cache write), K-2 (skill provenance real `user_uuid`, `skillBank.ts:339`), K-14 (clamp Kalman `U` to [0,1], `kalman.ts:47`), K-8 (driftProbe `AND` not `OR` on deleted/tombstoned, `driftProbe.ts:242`).

> **Sequencing note:** K-4 and K-7 share the `blocks` owner-lookup that K-6 also needs — land all three ACL/owner fixes in one commit and cache the lookup once. K-3 (compose layer fail-fast) should land **after** the manifest gains an explicit `optional: true` marker per layer, or it will hard-fail composes that legitimately tolerate a missing enrichment layer.

---

## Section 3 — v0.x+1 Architectural Shifts (P2)

Bundled by theme. Each bundle names total eng-weeks, prerequisite P1s, and risk-if-deferred.

### Bundle A — LLM-dispatch: route GEPA prompts through the harness ★ (4-lens consensus)
**Findings:** G-5, G-6, K-9, K-10, D-6, Opus Seam B. **Eng-wks:** 1.5. **Prereq P1s:** none. **Risk if deferred:** the entire prompt-evolution arc (GEPA mutation/reflection, VISTA gates, judge rubric, balanced-eval) emits inline `prompt: \`...\`` literals — only **1 of 44** `promptEvolution/*` files calls `registerPrompt` (`adversarialDrill.ts:63`; G-5). The 3-tier override chain (document→user→registered default) is **inert** for prompt-evolution, and quarterly reruns bypass the semantic/idempotency caches (D-6). This is also a direct violation of the project's mandatory harness rule. Migrate `gepaCron.ts:206/:367`, `gepaReflector.ts`, `judgeRubricService` through `registerPrompt` + `resolvePromptForDocument`; key candidate JSON by `{promptSlug, parentVersion, sampleSetHash, baselineHash}` (D-6) to claw back 80-100% on replayed cycles.

### Bundle B — Data-layer: close Seam B, make memory a budgeted compose layer ★
**Findings:** O-R2, O-R3, G-11, K-1. **Eng-wks:** 2-3. **Prereq P1s:** K-1 (fix the read-after-write break first, or you fold a broken store into compose). **Risk if deferred:** memory reaches the model as an *opaque prose splice* (`{{relevant_memory}}` at `promptIntegrationService.ts:1409`), outside the compose token budget and untraced (Opus §8). The FSRS/Kalman scoring machinery is **built but dormant** — `semantic.ts:149` ranks by score *if* `score_metadata` is supplied, but `compose.ts:495` never supplies it, so it always falls through to flat cosine (Opus §3). Thread `score_metadata` into `loadSemantic` (O-R2), promote the splice to a `loadMem0()` compose layer (O-R3). This is also the **scale fix**: Opus §6 shows the remote OpenMemory REST hop is the first thing to break under 10× load — folding it into a Postgres-local layer solves wiring + latency together.

### Bundle C — Runtime: batch structured-output + budget envelope
**Findings:** D-arch1, D-arch2, D-1, D-2, D-8, D-10, D-12, D-15. **Eng-wks:** 2. **Prereq P1s:** none. **Risk if deferred:** GEPA VISTA scoring (`vistaGate.ts:322`), LLM-judge (`llmJudgeCron.ts:341`), balanced-eval (`balancedEvaluation.ts:114`), memory calibration (`memoryExtractionProcessor.ts:318`), and multimodal indexing (`pdfLayoutIndex.ts:102`) all loop serially over records that share a rubric/system prompt. A single `llm.generateStructuredBatch({items, sharedPrompt, itemSchema, maxConcurrency})` helper (D-arch2) is the largest cross-cutting save — 50-80% input-token reduction on repeated framing — and a gateway-level budget contract (D-arch1) caps surprise spend before dispatch, not in nightly cron.

### Bundle D — Wire the end-to-end evolution + chapter paths
**Findings:** K-11, O-R5, O-R6, D-9. **Eng-wks:** 1.5. **Prereq P1s:** Bundle A (harness) for K-11's prompt path. **Risk if deferred:** `runEvolutionPipeline` logs "shadow testing" but **never creates the experiment or schedules `concludeAfter`** (K-11, `evolutionOrchestrator.ts:254`) — accepted candidates are never promoted, so the loop GEPA *looks* like it closes is open. Two dead chapter-compose implementations sit next to the live triad path (O-R5: `bookOrchestratorContextFirst.ts:76` + `chapterGenerate.ts:49`) — pick one, wire it, delete the other in the same commit (Pre-Prod posture). Balanced-eval emits a synthetic `score:0.5` verdict when all judges fail (D-9, `balancedEvaluation.ts:175`) — treat zero/under-quorum as a hard failed eval, never a promotable verdict.

---

## Section 4 — Long-Horizon (P3 + advisory)

- **System-of-record decision (O-R4)** — `CLAUDE.md` says "Postgres is the SoR," yet trajectory memory lives only in remote OpenMemory. This is the *parent* of Bundle B and Section 5's hardest question — defer the decision, but track it as load-bearing for any 1000× plan.
- **Config-surface consolidation (G-7★, O-R11★, O-R9★)** — move memory toggles (`BCE_M1_MEM0`, `OPENMEMORY_HOST`, `BCE_M1_PERSIST`) out of the SealedSecret into `values.yaml backend.env`, reserving the secret for genuine secrets (`MEM0_API_KEY`). De-dup `BCE_M3_CALIBRATOR` to one home (lives in *both* `values.yaml:299` and the secret — a drift trap where patching the secret silently does nothing because explicit `env:` wins).
- **Dead-surface cleanup (G-9, G-18, G-19, G-22)** — the `promptEvolution/index.ts` barrel is imported by zero production code (3 test files only); 8 contextEngine sub-modules (`kalman/fsrs/hebbian/score/jit/dualRoute/sessionDelta/composeHint`) have zero cross-folder callers. Internalize behind `__internal__/` or accept as library-style surface — but they inflate the jest module-graph load today.
- **Naming collisions (G-14, G-21)** — two `calibrationGate.ts` (GEPA canary vs compose read-gate) and `concludeShadowTest.ts` (production code matching the `*.test.ts` jest glob). Rename to disambiguate before a deep-import lands on the wrong one.
- **Dual red-team path (G-2)** — chart cron runs `redTeamDrill.js` weekly while `redTeamCron` registers a BullMQ quarterly scheduler for the same work; two paths drift and one is already broken (G-1). Pick one.

---

## Section 5 — Hardest Open Question

**Inherited from Opus §9:** *Should memory be a Context-Engine compose layer (Postgres-local) or stay a separate remote forgetting-curve service (OpenMemory)?*

Opus sketches the dependency: the answer **hinges on what `OPENMEMORY_HOST` actually points at** — a rich managed Mem0 (vector recall, entity resolution, cross-session consolidation) or a localhost stub. If managed → thin-sync that mirrors recalls into a CE layer for ranking, leaving consolidation remote. If stub → fold in wholesale.

**My assessment: the three sketched mitigations are necessary but NOT sufficient — more research is required, and it is cheap research.** Opus correctly notes the SealedSecret value is unreadable from a read-only repo audit, but the resolution is one authorized command away and does **not** need the secret plaintext:

1. `kubectl exec deploy/libwit-backend -n researcher -- printenv OPENMEMORY_HOST` (read-only, pre-authorized jisawru scope) reveals host shape — managed DNS vs `127.0.0.1`.
2. `kubectl get pods -n researcher | grep -i openmemory` + a `curl` to `$OPENMEMORY_HOST/health` from inside the pod tells you if it's a real service with real row counts.
3. A `SELECT count(*) FROM block_memory_open_set` vs the OpenMemory row count quantifies how much consolidation would have to be re-implemented.

Until those three probes run, **any commitment to Bundle B's R3/R4 is premature** — you'd be choosing a substrate shape blind. This is the single highest-leverage unknown in the tri-subsystem design, and it's resolvable in ~20 minutes of pod introspection. **Recommendation: run the three probes before scheduling Bundle B; the probe result selects thin-sync vs wholesale-fold.**

---

## Section 6 — Dogfood Reflection (meta-loop check)

**Was this audit reproducible via the framework?** Yes — 4 heterogeneous lenses dispatched in parallel, each produced a non-empty grep-anchored report, and synthesis cross-validated 6 consensus defects. The framework's value showed precisely where lenses *disagreed in emphasis but agreed on fact*: Codex called chart-drift "mixed, not uniformly broken" (`lens-codex.md:112`) and was right to caution that SealedSecret may carry `BCE_M1_MEM0` — GLM and Kimi flagged the same flags as "missing from values.yaml" without that nuance. The synthesis preserves Codex's caveat: **chart-absence is necessary but not sufficient proof of empty prod pods.** That tension is the audit working as designed.

**Did any lens get blocked by something the audit itself identified?** Yes — a genuine meta-loop hit. Opus's hardest question (§9) is blocked by the **same config-surface fragmentation the audit flags as a P3 finding** (O-R11): because memory toggles live in the SealedSecret rather than `values.yaml`, the architectural lens *cannot read from the repo whether OpenMemory is real or a stub*. The audit's own finding (move flags off the SealedSecret for reviewability) is exactly what would have let the audit answer its hardest question without a live probe. The reviewability regression O-R11 names is not hypothetical — it blocked this very audit.

**One honest gap:** every anchor is grep-derived against the working tree but **not** runtime-verified. G-15 (the documented-but-maybe-absent `MemoryUnavailableError` throw at `bookMemoryService.ts:772`) and the exact line drift in Opus's sub-agent-sourced anchors carry a "verify line still current" caveat. No lens ran the DB or an LLM — by design (read-only), but it means SM-2/FSRS math claims (K-14 clamp) are reasoned, not measured.

---

## Section 7 — How to Re-Run

```bash
# From the audit worktree root:
cd /Volumes/docker-ssd/Migration/Development/researcher-gepa-mem-ce-audit

# Re-dispatch the 4-lens refactor-audit recipe (mini-ork):
bin/mini-ork run refactor-audit \
  .mini-ork/kickoffs/gepa-mem-ce-integration-audit.md \
  --lenses glm,kimi,codex,opus \
  --budget-usd 40

# Verifier (confirms all 4 lens reports + synthesis anchors/consensus/missing-integration):
MINI_ORK_RUN_DIR=.mini-ork/runs/run-1782379484-31281 \
  bash verifiers/lens-completeness.sh
```

**P1 that blocks clean self-dispatch:** none in this run — all 4 lenses completed and the synthesizer dispatched normally. **Caveat for re-run:** per standing directives, **drop GLM and codex from mini-ork lane assignment** (GLM 429s on Fair Usage within 2-3 dispatches/hour; codex is banned in mini-ork lanes) — the lens *names* here are historical labels; the GLM lane already ran as MiniMax. Substitute `minimax,kimi,opus,sonnet` for a clean re-run.

---

## Appendix — Missing-Integration Ledger (built-but-unwired)

Every component below is implemented and type-checked but has **no live data path**. Grep call-site evidence inline.

| # | Component | File:line | Evidence of no wiring | Lens |
|---|---|---|---|---|
| 1 | `redteam_quarterly` handler | `promptEvolutionCronDispatch.ts:33` / `:96` | Enqueued to `scheduled-pipelines` but absent from `PROMPT_EVOLUTION_PIPELINE_TYPES` **and** `BCE_PIPELINE_TYPES` → `isPromptEvolutionPipelineType` false → silent `{success:true, itemsProcessed:0}` (`:76-82`) | G-1, G-10 |
| 2 | Shadow experiment scheduling | `evolutionOrchestrator.ts:254` | Logs "shadow testing" but never calls `experimentService.createShadowExperiment` / sets `concludeAfter` → accepted candidates never promoted (loop is open) | K-11 |
| 3 | Chapter-level compose | `bookOrchestratorContextFirst.ts:76-115` + `chapterGenerate.ts:49-84` | `getChapterContextFromEngine` / `composeChapterContext` exported, called **only by a test**; live path uses `loadChapterTriadContext` (`:2113`) | O-R5 |
| 4 | Prompt harness in GEPA | `promptEvolution/*.ts` (43/44 files) | Only `adversarialDrill.ts:63` calls `registerPrompt`; `resolvePromptForDocument` has 0 real calls (2 JSDoc-only hits in `pipelineProxySignals.ts:160,193`) | G-5, G-6, K-9, K-10, D-6 |
| 5 | FSRS/Kalman compose ranking | `semantic.ts:149` ← `compose.ts:495` | `loadSemantic` ranks by `score_metadata` **if supplied**; compose never supplies it → always flat cosine. Forgetting-curve writes have zero read leverage | O-R2, G-11 |
| 6 | Mem0 ↔ Context Engine bridge | `compose.ts` (whole file) | **Zero references** to `bookMemoryService`; only bridge is the `{{relevant_memory}}` string splice at `promptIntegrationService.ts:1409` | O-R3 (Seam B) |
| 7 | `ChapterWritingContract` read-path | `bookOrchestratorContextFirst.ts` | `loadLatestContract` called only by the repair loop; live path rebuilds fresh every run → write-path with no hot read-path | O-R6 |
| 8 | `FEATURE_MEMORY_HARD_FAIL` throw | `bookMemoryService.ts:104,118` (doc) vs `:772` | Comments promise `MemoryUnavailableError` throw under hard-fail; no grep-confirmed `throw` site | G-15 |
| 9 | `handleRedTeamDrill` | `redTeamCron.ts:309` | Called only from the dead queue path (#1) and a manual endpoint — no live worker invocation | G coverage table |

### FEATURE_* / BCE_* chart-mirroring table

Canonical source: `infra/charts/libwit/values.yaml` backend.env vs the SealedSecret (`templates/sealed/libwit-server-env.yaml`). **"present-in-chart"** = explicit in `values.yaml backend.env`. *Caveat (Codex `lens-codex.md:112`): chart-absence is necessary, not sufficient, proof of empty prod — SealedSecret or `kubectl set env` may carry the value.*

| Flag | In `values.yaml`? | In SealedSecret? | In `.env.example`? | Prod reality | Action |
|---|:---:|:---:|:---:|---|---|
| `FEATURE_PROMPT_EVOLUTION` | ✅ `:307` | ❌ | ❌ | Crons register (Seam A producer ON) | OK |
| `BCE_M3_CALIBRATOR` | ✅ `:299` | ✅ | ❌ | **Dual home** — `env:` wins, secret patch is a no-op trap | De-dup (O-R9) |
| `BCE_M1_MEM0` | ❌ | ✅ `:42` | ✅ | Set via secret (encrypted, unreviewable) | Move to chart (G-7, O-R11) |
| `OPENMEMORY_HOST` | ❌ | ✅ `:145` | ✅ | Sealed → cannot assert real host | Move to chart (O-R11) |
| `BCE_M1_PERSIST` | ❌ | ✅ | — | Set via secret | Move to chart |
| `BOOK_CONTEXT_FIRST` | ❌ | ✅ | — | Chapter path ON via secret | Move to chart |
| `FEATURE_MEMORY_HARD_FAIL` | ❌ | ❌ | ❌ | **Absent everywhere → fail-open no-op in prod** | Mirror + decide posture (G-3★, O-R8★) |
| `FEATURE_LLM_JUDGE_CRON` | ❌ | ❌ | ❌ | **Absent everywhere → nightly judge OFF in prod** | Mirror (G-4) |
| `FEATURE_BOOK_EVENT_DRIVEN_DISPATCH` | ❌ | ❌ | ❌ | **Absent → newest feature (commit `0bfc4df7e`) dark in prod** | Mirror in shipping PR (O-R10) |
| `BCE_M2_CALIBRATION_AUDIT_CRON` | ✅ `:325` (`'false'`) | ❌ | ❌ | Manual flip only; no auto-trigger despite "flip once enough rows" comment | Startup probe or runbook (G-13) |
| `CE_T2_ENFORCE` | ❌ | — | ✅ | T2 agent/distiller writes log-allowed only | Mirror (K-table) |
| `CE_T3_ENABLED` | ❌ | — | ✅ | Cross-scope agent writes always denied | Mirror (K-table) |
| `HEBBIAN_TOPK` / `CONTEXT_CACHE_TTL_SECONDS` / `FSRS_STABILITY_FLOOR_DAYS` | ❌ | — | ✅ | Hardcoded defaults; chart cannot tune | Mirror (K-table) |
| `FEATURE_HARNESS_PLANNER_WEIGHTS` | ✅ (`'false'`) | ❌ | ❌ | Chart only | OK |

**The three flags that are dark in prod with operational consequence** (absent from *every* surface, not just the chart): `FEATURE_MEMORY_HARD_FAIL`, `FEATURE_LLM_JUDGE_CRON`, `FEATURE_BOOK_EVENT_DRIVEN_DISPATCH`. These are not chart-drift (where a secret might carry the value) — they are *genuinely unset*, so the consuming code takes its `undefined`-default branch in prod regardless of operator intent.
