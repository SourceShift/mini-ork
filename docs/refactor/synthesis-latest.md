# Synthesis — GEPA × Memory × Context-Engine Integration Audit

**Scope.** Read-only 4-lens audit of three subsystems in the researcher repo —
GEPA (prompt evolution), Memory (Mem0 triad + book-gen distillation), and the
Context Engine (block-scoped 6-layer compose) — scored primarily on
**missing/broken integration wiring**, secondarily on correctness + Zero-Fallback
violations.

> **Lens degradation notice (read first).** This audit ran **2 of 4 lenses**.
> `lens-glm` (MiniMax lane) and `lens-kimi` both hard-failed at dispatch on
> **missing API keys** — `MINIMAX_API_KEY` and `KIMI_API_KEY` are unset in
> `~/ps/mini-ork/config/secrets.local.sh` (see `llm-failures/1782377764-{minimax,kimi}.err.log`).
> The synthesis below is built from **codex** (`lens-codex.md`, 15 cost/wiring
> findings) and **opus** (`lens-opus.md`, R1–R7 architectural). Findings are
> prefixed `D-N` (codex) and `O-RN` (opus). The G-/K- columns of the matrix are
> empty by failure, not by absence of findings — see §6 Dogfood reflection,
> which treats this as a live data point, not a footnote.

All anchors below were re-verified by grep against the worktree before publish.

---

## Section 1 — Severity × leverage matrix

Findings prefixed by lens (`D-N` codex, `O-RN` opus). **★** = consensus
(surfaced by **2+ lenses**). G-/K- cells blank because those lenses died.

| | **HIGH leverage** | **MED leverage** | **LOW leverage** |
|---|---|---|---|
| **P1** | **O-R1** GEPA two promotion paths, gated one orphaned ★(O-R7 thematic) · **D-14 ★ O-R3** mem flags off the auditable chart surface | **D-1** judge cron 1 call/execution ($170-255/mo) · **D-15** `FEATURE_LLM_JUDGE_CRON` not in chart | **D-10** distill LLM before cheap reject guard |
| **P2** | **O-R5 ★ D-11** calibrator: no recall-time correction + no rolling cost cap · **D-6** compose-hint silent fall-open to all layers (Zero-Fallback) | **D-2** VISTA gate serial baseline+candidate · **D-9** trajectory extract 1 call/row · **D-3** GEPA reflection 1 edit/call · **O-R6** contract built alongside compose, not from it | **D-7** multimodal not actually in the parallel `Promise.all` |
| **P3** | **D-Arch1** batch-first LLM gateway (substrate) | **D-4/D-5** GEPA bypasses semantic cache; cache is exact-hash-gated · **D-12** write classifier embeds before dedupe · **D-13** fixture gate resends template/fixture | **O-R2** canary label written, maybe never read · **O-R4** second recall reader gate-parity · **D-8** HYPE no fact-level idempotency |

**Consensus signals (2+ lenses):**
- **D-14 ★ O-R3** — Memory/judge `FEATURE_*`/`BCE_*` flags absent from
  `values.yaml`. Both lenses independently flagged the chart-drift surface as
  the #1 *integration* defect. Strongest consensus in the audit.
- **O-R5 ★ D-11** — The calibrator is half-wired. Opus: no **recall-time**
  bias correction. Codex: no **rolling cost cap** on `calibrate()`. Same
  component, two distinct holes → consensus that the calibrator integration is
  incomplete.
- **O-R1 ↔ O-R7** — single-lens (opus) but internally reinforced: the
  self-evolution promise is gated + governance-split.

---

## Section 2 — Top 5 immediate wins (P1, total < 2 weeks)

| # | ID | Title | Lens | One-line fix | Effort |
|---|---|---|---|---|---|
| 1 | **D-14 ★ O-R3** | Mirror mem flags to the auditable chart surface | codex+opus | Add `BCE_M1_MEM0`, `OPENMEMORY_HOST`, `FEATURE_MEMORY_HARD_FAIL`, `FEATURE_LLM_JUDGE_CRON` to `infra/charts/libwit/values.yaml` `backend.env` with explicit on/off (host is not a secret) | 0.5 d |
| 2 | **O-R1** | Unify GEPA's two promotion writers | opus | Make `concludeShadowTest` call `promoteCandidate` gates before `setLabel('production')`, OR delete `promotionChecklist.ts` + its CLI script and document shadow-test as authoritative | 2-3 d |
| 3 | **D-10** | Move distill reject-guard before the LLM call | codex | Hoist `validateDistillInput()` above `generateStructured()` in `skillDistiller.ts` — near-100% savings on rejected candidates, zero behavior change | 0.5 d |
| 4 | **D-6** | Kill compose-hint silent fall-open (Zero-Fallback) | codex | On hint JSON parse-fail at `composeHint.ts:171`, throw or use a deterministic classifier — do not silently expand to all layers | 1 d |
| 5 | **D-1** | Batch the LLM judge cron | codex | 5-10 executions per structured judge prompt with per-item IDs — 70-85% judge-token cut, ~$170-255/mo | 2-3 d |

Sum ≈ 6.5-8 eng-days. Win #1 is the keystone: until the flags are on the
auditable surface, **nobody can tell from the repo whether the Mem0 triad and
judge cron are live in prod** — every other finding is downstream of that blind
spot.

---

## Section 3 — v0.x+1 architectural shifts (P2, by theme)

**Bundle A — LLM-dispatch (substrate).** `D-Arch1` batch-first gateway
`generateStructuredBatch({sharedPrompt, items, schema})` routing judge (D-1),
VISTA samples (D-2), fixture regression (D-13), trajectory extraction (D-9), and
GEPA edit application (D-3) through one "same prompt, N payloads" path.
**Total: ~3 eng-wk. Prereq P1s:** D-1 (proves the shape). **Risk if deferred:**
eval/reflection cost stays linear in fanout — D-11's unbounded calibrator retry
storm compounds it.

**Bundle B — Memory read-path completion.** `O-R5`+`D-11` recall-time
calibration pass + rolling per-user/job cap before `recallRelevantMemory` output
enters the chapter contract. `O-R4` gate-parity audit of the second reader
(`memoryAsToolService.ts`). **Total: ~2 eng-wk. Prereq:** Win #1 (flags
observable). **Risk if deferred:** un-calibrated, drift-stale memory silently
enters chapter context once `BCE_M1_MEM0` flips on — a correctness regression
hiding behind a green write→read loop.

**Bundle C — Context Engine seams.** `O-R6` make `ChapterWritingContract` a
*projection* of `composeBlockContext` rather than a sibling assembled from
`synthesis_result`; `D-7` move multimodal into the expensive-layer `Promise.all`.
**Total: ~2 eng-wk. Prereq:** none. **Risk if deferred:** compose view and
contract diverge under load; p95 visual-query latency stays additive.

**Bundle D — GEPA cache governance.** `D-4`/`D-5` add an approved
semantic/advisory reuse mode for deterministic eval prompts; cost-gated tier
router so `escalation` is reached only on confidence-threshold failure.
**Total: ~2 eng-wk. Prereq:** O-R1 (promotion unified first, so cached
reflections can't promote via the ungated path). **Risk if deferred:** retry
storms + crash-resume re-pay full reflection cost.

---

## Section 4 — Long-horizon (P3 + advisory)

- **O-R2** — `setLabel` supports `'canary'|'staging'` but
  `resolvePromptForDocument` does a single `label='production'` lookup. Either
  the canary allocator computes splits nothing reads, or splitting happens
  upstream. Track until traffic-split is needed.
- **D-8** — HYPE enrichment has no `open_set_uuid`/fact-hash idempotency;
  reindex re-pays. Matters at reconcile scale.
- **D-12** — write classifier embeds `JSON.stringify(payload)` before neighbor
  dedupe; add short-TTL exact-hash cache. Matters under replay/double-click load.
- **Scale ladder (opus §6):** at 100× the `prompt_versions WHERE
  label='production'` per-resolve probe + per-block compose fan-out bite; at
  1000× `block_memory_open_set` is unpartitioned and OpenMemory is single-host.
  Context Engine is built to scale (async queues, Zero-Fallback throws); GEPA +
  Mem0 are built to *ship-dark*.

---

## Section 5 — Hardest open question (inherited from Opus §7)

**Does the GEPA shadow-test statistical gate (`concludeShadowTest`) actually
subsume the promotion-checklist gates (`promotionChecklist`), or are
rollback-guard / calibration-gate checks being silently skipped on every
automatic promotion?**

This is the audit's sharpest finding and it is **unanswerable read-only** —
correctly so. Verified state:

- **Path A (live, ungated):** `concludeShadowTest.ts:280` calls
  `setLabel(pool, version_uuid, 'production')` then flips run status to
  `'promoted'` at `:289`. Runs automatically under `FEATURE_PROMPT_EVOLUTION`.
- **Path B (gated, orphaned):** `promoteCandidate` at
  `promotionChecklist.ts:189` flips the same label *only after all gates pass* —
  but grep confirms its **only** non-test caller is the CLI script
  `server/scripts/promoteCandidate.ts`. No cron, processor, or route invokes it.

**Assessment of the three mitigations:** Opus sketches (a) inline the gate set
into `concludeShadowTest`, (b) delete `promotionChecklist` + document shadow-test
as authoritative, (c) trace one real promotion through Loki for
`rollbackGuard`/`calibrationGate` log lines. **(c) is necessary before either
(a) or (b) — and it is not optional research, it is the deciding evidence.** You
cannot choose between "the checklist is redundant" (→delete) and "the checklist
is load-bearing and bypassed" (→inline) without knowing whether real promotions
ever exercised the gates. **More research IS needed**, and it is a one-hour Loki
query against a `FEATURE_PROMPT_EVOLUTION=true` window — not a code change.
Until that trace runs, treat every prod prompt promotion as **gate-bypassed by
default** and freeze auto-promote if the loop is hot. The wiring is the disguise:
`resolvePromptForDocument` reads `label='production'` correctly, so the system
*looks* integrated end-to-end. The defect is one layer up — in *who is allowed
to write that label.*

---

## Section 6 — Dogfood reflection (meta-loop check)

**Was this audit reproducible via the framework? Partially — and it broke on
exactly the failure class it was hired to find.**

The audit's thesis is "built components silently no-op because an *enablement
surface* (a flag, a key, a wire) is missing." The audit's own dispatch then
**no-op'd two of four lenses because two API keys (`MINIMAX_API_KEY`,
`KIMI_API_KEY`) are missing from `secrets.local.sh`** — a credential surface
that, like `BCE_M1_MEM0` in `values.yaml`, is invisible until you grep the
failure log. The framework dispatched all four lanes; two returned empty and the
pipeline proceeded **without failing loud** on the gap — the same
silent-degradation pattern D-6 flags in `composeHint.ts:171`. The audit caught
its own anti-pattern in the mirror.

What survived was load-bearing: codex + opus are the two lenses whose mandates
(LLM-dispatch/wiring, architectural shape) most directly serve the
integration-completeness dimension, so coverage of the **primary** scoring axis
is intact. What was lost: GLM's fast tactical breadth sweep and Kimi's
line-level correctness/SQL-drift deep-read — meaning the **secondary** axes
(correctness bugs, race conditions, SQL param-typing) are **under-covered**.
Treat the matrix's P2/P3 correctness cells as provisional.

**Honest gap:** with only 2 lenses there is less cross-lens redundancy, so
consensus markers (★) are scarcer and the single-lens findings (most of opus
R1/R2/R4/R6) carry no second-lens confirmation. They are grep-verified but not
*independently* corroborated.

---

## Section 7 — How to re-run

The blocking P1 for self-reproduction is **the missing lens API keys**, not a
code defect:

```bash
# 1. Fix the dispatch-level blocker (the meta-loop P1):
#    add to /Volumes/docker-ssd/ps/mini-ork/config/secrets.local.sh
export MINIMAX_API_KEY=...   # lens-glm lane
export KIMI_API_KEY=...      # lens-kimi lane

# 2. Re-dispatch the 4-lens refactor audit (read-only):
cd /Volumes/docker-ssd/Migration/Development/researcher-gepa-mem-ce-audit
~/ps/mini-ork/bin/mini-ork run refactor-audit \
  <kickoff-with-the-plan.json>   # task_class: refactor_audit, budget $40 default

# 3. Verify completeness:
bash .mini-ork/runs/<run-id>/verifiers/lens-completeness.sh
```

Without step 1, the audit reproduces at **2/4 lens coverage** exactly as this
run did. The framework is otherwise reproducible: plan → 4 parallel lenses →
synthesize → verify → publish ran clean for the surviving lanes.

---

### Appendix A — Missing Integration (built-but-unwired components)

Every entry has grep call-site evidence. This is the audit's primary deliverable.

| # | Component | Built at | Wired? | Evidence | Lens |
|---|---|---|---|---|---|
| 1 | `promoteCandidate` (gated promotion) | `promotionChecklist.ts:189` | **NO** — only caller is CLI script | grep `promoteCandidate` → 3 files: impl, `scripts/promoteCandidate.ts`, test. No cron/route/processor. | O-R1 |
| 2 | `BCE_M1_MEM0` / `OPENMEMORY_HOST` (whole Mem0 triad gate) | gate `bookMemoryService.ts:191` | **NO on auditable surface** — sealed only | `grep -c 'BCE_M1_MEM0\|OPENMEMORY_HOST' infra/charts/libwit/values.yaml` → **0**; present only at `sealed/libwit-server-env.yaml:42,145` | D-14 ★ O-R3 |
| 3 | `FEATURE_LLM_JUDGE_CRON` | read `cron/index.ts:53` | **NO** — not in chart | absent from `values.yaml` (grep confirmed: only `BCE_M3_CALIBRATOR:299`, `FEATURE_PROMPT_EVOLUTION:307`) | D-15 |
| 4 | `FEATURE_MEMORY_HARD_FAIL` | read `memoryHealthProbe.ts:35` | **NO** — not in chart | absent from `values.yaml` | D-14 |
| 5 | recall-time `calibrate()` correction | `calibrationGate.ts:78`, `bookMemoryService.ts:741` | **WRITE-ONLY** — gates writes, never re-scores recall | `recallRelevantMemory` (`bookOrchestratorContextFirst.ts:3420`) has no `calibrate()` before consumption; only a *comment* at `:3872` | O-R5 ★ D-11 |
| 6 | `semanticPromptCache` get/store | `semanticPromptCache.ts:237` | **NOT BY GEPA** — judge/reflection paths skip it | grep `getCachedResponse`/`storeCachedResponse` — GEPA judge/reflection paths pay full LLM cost | D-5 / D-4 |
| 7 | canary/staging label fan-out | `setLabel` supports it, `promptIntegrationService.ts:1319` | **WRITE-WITHOUT-READ?** | resolve does single `label='production'` lookup at `:1288`; no canary fan-out at resolve | O-R2 |
| 8 | multimodal "parallel branch" | comment at `compose.ts` claims parallel | **NO** — awaited serially | semantic+graph in `Promise.all` (`compose.ts:487`), multimodal awaited later at `:530` | D-7 |

### Appendix B — Flag chart-mirroring verdicts (grep-verified)

| Flag | In `values.yaml`? | Anchor / verdict |
|---|---|---|
| `FEATURE_PROMPT_EVOLUTION` | **YES** | `values.yaml:307` = `'true'` |
| `BCE_M3_CALIBRATOR` | **YES** | `values.yaml:299` = `'true'` |
| `BCE_M1_MEM0` | **NO** | sealed-only `sealed/libwit-server-env.yaml:42` — prod state unobservable from repo |
| `OPENMEMORY_HOST` | **NO** | sealed-only `sealed/libwit-server-env.yaml:145` — host is not a secret; should be plaintext |
| `FEATURE_MEMORY_HARD_FAIL` | **NO** | code-read `memoryHealthProbe.ts:35`, absent from chart |
| `FEATURE_LLM_JUDGE_CRON` | **NO** | code-read `cron/index.ts:53`, absent from chart — $300/mo judge lane governed off-surface |
