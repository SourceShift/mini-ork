# Synthesis — GEPA / Memory / Context-Engine Integration Audit

**Run:** `run-1782378483-66819` · **Date:** 2026-06-25 · **Stance:** read-only, integration-completeness first.

> **Lens coverage: 2 of 4 landed.** `lens-codex.md` (LLM-dispatch/cost) and
> `lens-opus.md` (architectural shape) completed. **`lens-glm` and `lens-kimi`
> never ran** — both died at launch on missing API keys
> (`KIMI_API_KEY`, `MINIMAX_API_KEY` unset in
> `MINI_ORK_HOME/config/secrets.local.sh`, see
> `.mini-ork/runs/run-1782378483-66819/llm-failures/1782378577-{kimi,minimax}.err.log`).
> minimax was the glm-substitute (per the standing skip-GLM rule). **This is a
> meta-loop hit** — the audit was itself blocked by the exact config-drift /
> unmirrored-secret class it set out to find. Consensus signal below is
> therefore weaker than a 4-lens design: where Codex and Opus *independently*
> converge, that is strong; single-lens findings are NOT down-weighted but ARE
> un-triangulated. See §6.

Finding IDs: `D-N` = Codex lens, `O-RN` = Opus lens. Consensus (2+ lenses) = ★.

---

## Section 1 — Severity × leverage matrix

Severity: **P1** = load-bearing now (correctness / prod-starvation), **P2** =
v0.x+1 architectural, **P3** = tracked, not load-bearing. Leverage = blast radius
÷ how much it unblocks. (Opus's "P0" R1 sits at top-of-P1 here.)

| | HIGH leverage | MED leverage | LOW leverage |
|---|---|---|---|
| **P1** | **O-R1 ★** (un-starve GEPA, 1 chart line) · **O-R7** (SM-2→compose bridge) · **O-R5** · **O-R6** | **O-R3** (SM-2 kernel) · **O-R2** (boot flag-assert) | — |
| **P2** | **D-9** ($700/d citation batch) · **D-8** ($600/d judge cascade) · **D-4 ★** ($450/d judge batch) | **D-1/D-2/D-3** (distill batch+policy) · **D-13 ★** (guard facade) · **O-R8 ★** (fail-loud catches) · **O-R4** (typed import) | **D-11/D-12** (tier:'tag' routing) · **D-10** (calibrator cap) |
| **P3** | — | **D-5/D-6/D-7** (deep GEPA batch chain) · **D-14** (HYPE cost visibility) | store-collapse decision (§5) |

**Consensus cells:**
- **★ `llmJudgeCron.ts` is a load-bearing weak point** — surfaced by *both*
  lenses via *different* failure modes: Opus O-R1 (flag-starved →
  `llmJudgeCron.ts:341` never scores) + Codex D-4 (when it *does* run, it's a
  serial per-execution loop, `llmJudgeCron.ts:341`/`:498`). Same component, two
  orthogonal defects → highest-confidence target in the audit.
- **★ Silent-degradation in the LLM dispatch path** — Codex D-13 (GEPA/CE hot
  paths bypass `withGuards`, `server/llm/middleware/index.ts:4`) + Opus O-R8
  (resolver `catch { return null }`, `promptIntegrationService.ts:1266`). Both
  name the same Zero-Fallback-violating shape from opposite ends (missing guard
  vs. swallowing catch).
- **★ Chart flag-mirroring** — both lenses independently ran the same
  flag-vs-chart diff and corroborate on the *present* flags at identical
  anchors (`infra/charts/libwit/values.yaml:299` `BCE_M3_CALIBRATOR`, `:307`
  `FEATURE_PROMPT_EVOLUTION`; Codex "what's already right" #7 = Opus §6 table).
  Opus extends it to the *missing* flags. Convergent methodology, complementary
  coverage.

---

## Section 2 — Top 5 immediate wins (P1 / highest-ROI) — total < 1 week

Ranked by ROI (severity × leverage ÷ effort), not lens order.

| # | ID | Title | Lens | One-line fix | Effort |
|---|---|---|---|---|---|
| 1 | **O-R1 ★** | `FEATURE_LLM_JUDGE_CRON` absent → GEPA promotion starves | Opus (+ Codex D-4 corroborates the component) | Add the flag (+ confirm `concludeShadowCron` gate) to `infra/charts/libwit/values.yaml` beside `:307`, with a loud `logger.info` on flag-OFF | 0.5 d |
| 2 | **O-R5** | `FEATURE_MEMORY_HARD_FAIL` unmirrored → Mem0 writes fail silent | Opus | Mirror to chart, default `'true'` (pre-prod fail-loud), read at `memoryHealthProbe.ts:35` | 0.5 d |
| 3 | **O-R6** | 3× `CONTEXT_*` crons unmirrored → FSRS decay never runs in prod | Opus | Mirror `CONTEXT_FSRS_DECAY_CRON` / `CONTEXT_RECONCILE_CRON` / `CONTEXT_ENGINE_EVAL_INTERVAL_MS`; decay is the load-bearing one (else `block_memory_open_set` grows monotonically) | 0.5 d |
| 4 | **D-8** | Balanced eval pays Flash+OpenRouter+Claude up-front | Codex | Cascade: Flash first → add OpenRouter only on low-confidence → Claude only on disagreement, `balancedEvaluation.ts:127`/`:150`/`:165` | 1–2 d |
| 5 | **D-11 + D-12** | Pure classifiers dispatch `tier:'fast'`, not `tier:'tag'` | Codex | Flip `verticalClassifierLLM.ts:256` + `autoAssignChapterSkills.ts:158` to `tier:'tag'` (64% input-cost cut, `tiers.ts:62`) | 0.5 d |

Sum ≈ **3.5–4.5 engineer-days**. #1–#3 are one-line chart edits behind a
verified consumer; ship them in a single PR + a smoke that greps the prod pod
env for each flag. #4–#5 are pure cost cuts (~$750/day combined at 100K-turn
scale per Codex's own estimates) with zero behavior change.

Bonus same-PR add: **O-R2** (boot-time assertion that the GEPA flag dependency
set is co-present) — fails loud at startup so a half-registered subsystem can
never silently recur. ~0.5 d.

---

## Section 3 — v0.x+1 architectural shifts (P2)

Bundled by substrate theme.

### Bundle A — data-layer: one write→store→read spine per memory class · ~3 eng-wks
- **O-R3** extract SM-2/FSRS recurrence into a single pure kernel
  (`server/services/memory/sm2Kernel.ts`); both `spacedRepetitionService.ts:430`
  and `spacedRepetitionScheduler.ts:62` call it. Today they are two disjoint
  re-implementations over `memory_items` vs. `user_chapter_progress`.
- **O-R7** (highest single-leverage item in the audit) add `layers/dueReview.ts`
  reading the unified store, injecting "overdue concepts" as an open-set fact
  class into `compose()`. This is the *only* change that makes the two memory
  subsystems actually one.
- **O-R4** replace the widened-module runtime reflection in
  `memoryExtractionProcessor.ts:74-217` with a typed
  `import { writeMemory }`; let `tsc` be the contract.
- **Prereq P1s:** O-R3 must land before O-R7 (R7 reads the kernel's store).
- **Risk if deferred:** the next off-by-one SM-2 fix lands in one engine and
  silently leaves the other wrong (Opus); "spaced-repetition-aware context"
  stays structurally impossible.

### Bundle B — LLM-dispatch: batch-first pipelines · ~2.5 eng-wks
- **Context distill** D-1 (STITCH per-fact, `distill.ts:202`→`stitch.ts:65`) +
  D-2 (op-classify per-fact, `distill.ts:227`→`operations.ts:224`) + D-3
  (`shouldDistillNow` always returns `true`, `distill.ts:284`) → fold into one
  batch job per turn/chapter. Codex est. **70–90% fewer CE LLM calls**.
- **GEPA** D-4 (judge batch ★) + D-5 (reflection patch batch,
  `gepaReflector.ts:326`) + D-6 (validation chain fuse,
  `validationPipeline.ts:103`) + D-7 (fixture batch, `fixtureRunner.ts:97`).
- **Citation** D-9 classify-per-chapter (`citationAuditService.ts:556`) with a
  content-hash read-through cache. Single biggest line item ($700/day).
- **Prereq P1s:** none — independent of Bundle A.
- **Risk if deferred:** linear/quadratic LLM spend scales with traffic; today's
  cost is invisible until volume arrives.

### Bundle C — runtime: central guarded facade + fail-loud · ~1.5 eng-wks
- **D-13 ★** move idempotency/throttle/circuit-breaker into `llm.generate*`
  defaults for named hot paths (`server/llm/middleware/index.ts:4`) instead of
  "helper exists, caller must remember." GEPA + CE currently bypass it.
- **O-R8 ★** replace bare `catch { return null }` at
  `promptIntegrationService.ts:1266` + `:1284` with a logged, metric-incremented
  catch that distinguishes "not found" (null) from "lookup failed" (alert).
- **D-3** enforce the distill policy knobs (gate before dispatch).
- **Risk if deferred:** a transient DB blip silently downgrades *every* prompt
  to its hard-coded default with no telemetry — degradation that looks like
  normal operation (Opus).

### Bundle D — observability: chart-drift + cost visibility · ~1 eng-wk
- **O-R2** boot-time flag-dependency assertion (also in §2 bonus).
- Loud `logger.info` on every gated flag-OFF (chart-drift class, per
  `FEATURE_PG19_EXEMPLARS` incident) so "starved" ≠ "no work."
- **D-14** include generation cost in HYPE's returned `totalCost`
  (`hype.ts:142` returns embedding-only) so budgets can actually stop dispatch.
- **D-10** add pre-dispatch cost estimate + `(memory_kind, normalized_tags,
  text_hash)` cache to the calibrator (`bookMemoryService.ts:817` warns *after*
  the call already happened at `:797`).

---

## Section 4 — Long-horizon (P3 + advisory)

- **D-5 / D-6 / D-7** deep GEPA batch chain — tracked behind Bundle B's
  higher-ROI distill+citation work; GEPA volume is quarterly-cron-bounded today.
- **D-14 / D-10** cost-visibility items — advisory until HYPE/calibrator paths
  carry real traffic.
- **Store-collapse decision** (§5) — the one multi-week call; explicitly *not*
  forced now. R7's read-only bridge defers it cleanly.
- Codex architectural #3 (operation-shape router with **enforcement tests** so
  `vertical_classifier` / `book-skill-assignment` / citation-class / memory-op
  default to `tag` unless justified) — lint/test scaffolding, low urgency but
  prevents D-11/D-12 regressions.

---

## Section 5 — Hardest open question

**Inherited from Opus §7: should the two SM-2 engines + the Mem0 layer collapse
into one store, or stay three?**

Opus sketches three mitigations: **(a) collapse** → one `memory_store`, R7
trivial; **(b) keep three** → respects the latency/ownership boundary, R7 via a
read-only cross-layer join; **(c) bridge via R7** regardless. Opus leans
*keep-three + unify-the-math + bridge*, held loosely.

**My assessment: the three mitigations are sufficient to defer the decision, not
to resolve it.** Unifying the *math* (O-R3) is unambiguously correct and
unblocks everything downstream — do it now, no further research needed. The
*store* decision should NOT be made on this audit's evidence, because the one
input that decides it was never gathered: **the compose latency budget.** R7's
read-only join (mitigation c) is cheap *only if* compose can afford one extra PG
read per turn within its existing budget — and `compose.ts` already does cache +
budget-paring + parallel fanout (`compose.ts:366`/`:447`, Codex), so the
headroom is measurable but unmeasured here. The genuinely open sub-question is
**product, not architectural**: must SM-2 mastery influence agent context in
*real-time* (forces collapse) or is *batch* acceptable (keep-three + R7 read
layer wins)? That is a one-experiment question — instrument compose's p95 with a
shadow `dueReview` join behind a flag, measure the delta, *then* decide. **More
research needed, but it is bounded and cheap** (one flagged read-only layer + a
latency probe), not a speculative multi-week migration. Recommendation: ship
O-R3 now, prototype O-R7 as read-only behind a flag, and let the latency number
make the collapse call.

---

## Section 6 — Dogfood reflection (meta-loop check)

**Was this audit reproducible via the framework? Partially — and it was blocked
by a defect of the exact class it audits.**

1. **2 of 4 lenses never ran.** `lens-kimi` and `lens-minimax` (the glm-slot
   substitute) both died at launch:
   `KIMI_API_KEY`/`MINIMAX_API_KEY` "is required - set it in
   `MINI_ORK_HOME/config/secrets.local.sh`"
   (`llm-failures/1782378577-{kimi,minimax}.err.log`). **This is the
   unmirrored-secret / config-drift class — the same failure mode the audit's
   own flag-mirroring section (O-R1/R5/R6) is built to catch.** The audit
   tripped on its own primary finding theme. Meta-loop: confirmed.
2. **Telemetry sink missing.** `trace-write-errors.log` shows three
   `sqlite3.OperationalError: no such table: llm_calls` — the run's own
   cost-ledger write-path has no table behind it. That is a write-path-with-
   no-read-target — *also* a finding class this audit hunts (cf. O-R7's SM-2
   write with no compose read). The framework instrumenting the audit exhibits
   the defect category under audit.
3. **Consensus is half-strength.** The recipe's value proposition is 4-stance
   triangulation; we got 2. Where Codex and Opus converge (the 3 ★ cells in
   §1) the signal is real and load-bearing. But Kimi (correctness deep-read:
   SM-2 off-by-one math, ACL fail-open, cache-key collisions, SQL param-typing)
   and GLM (tactical breadth grep-sweep) covered dimensions **neither surviving
   lens fully owns** — Codex is cost-shaped, Opus is shape-shaped. **The
   correctness-bug surface (Kimi's mandate) is the single biggest blind spot in
   this synthesis.** No SM-2 arithmetic was verified; O-R3's "they will drift on
   the next off-by-one" is asserted, not proven, precisely because the lens that
   would have proven it never ran.

**Verdict:** the *framework* is reproducible; *this run* was not clean. The
findings that survived are trustworthy (anchored, cross-checked where possible),
but the audit is **incomplete on correctness** and must be re-run at full lens
capacity before any P1 SM-2 math claim (O-R3 drift) is treated as verified.

---

## Section 7 — How to re-run

**Blocker (P1 for re-run):** populate the two missing keys, else you get the
same 2/4 degraded run:

```bash
# In $MINI_ORK_HOME/config/secrets.local.sh (NOT committed):
export KIMI_API_KEY=...        # unblocks lens-kimi (correctness deep-read)
export MINIMAX_API_KEY=...     # unblocks lens-minimax (glm-slot substitute)
```

Then re-dispatch the audit recipe:

```bash
cd /Volumes/docker-ssd/Migration/Development/researcher-gepa-mem-ce-audit
/Volumes/docker-ssd/ps/mini-ork/bin/mini-ork run research-synthesis \
  .mini-ork/runs/run-1782378483-66819/plan.json
# (recipe = 4 parallel lenses → synthesizer → verify-completeness → publish)
```

**Self-verification (verifier script `verifiers/lens-completeness.sh` was never
authored — planned in the plan, absent on disk).** Inline checks from the
`verifier_contract`, runnable now:

```bash
RUN=.mini-ork/runs/run-1782378483-66819
for l in codex opus; do test -s "$RUN/lens-$l.md" && \
  grep -qE '[A-Za-z0-9_/.-]+\.(ts|tsx|sql|sh):[0-9]+' "$RUN/lens-$l.md"; done
test -s "$RUN/synthesis.md" && \
  grep -qE '[A-Za-z0-9_/.-]+\.(ts|tsx|sql|sh):[0-9]+' "$RUN/synthesis.md"
```

The `lenses-exist-nonempty` and `each-lens-has-anchor` checks **pass for
codex+opus and FAIL for glm+kimi** (absent) — an honest red, not a papered-over
green. Author `verifiers/lens-completeness.sh` to encode this matrix and treat
a <4 lens count as a hard fail on the next run.

---

## Appendix A — Missing Integration (built-but-unwired, ≥5 with grep evidence)

Every entry: a component that exists in source with low/zero downstream wiring.

| # | Component | Built at | Wired? | Evidence | Lens |
|---|---|---|---|---|---|
| 1 | `FEATURE_LLM_JUDGE_CRON` (scores GEPA shadow runs) | consumer `llmJudgeCron.ts` processor | ❌ absent from chart | `grep -r FEATURE_LLM_JUDGE_CRON infra/charts/` → nothing | O-R1 (P0) |
| 2 | `FEATURE_MEMORY_HARD_FAIL` (enforces Zero-Fallback on Mem0) | read `memoryHealthProbe.ts:35` | ❌ absent from chart | flag-diff §6 | O-R5 (P1) |
| 3 | `CONTEXT_FSRS_DECAY_CRON` + reconcile + eval crons | `workers/unified/processors/contextFsrsDecay.ts` etc. | ❌ absent from chart | grep `infra/charts/` → nothing | O-R6 (P1) |
| 4 | SM-2 store → compose read path | `memory_items` / `user_chapter_progress` writers exist | ❌ never read by compose | `grep memory_items compose.ts layers/` → 0 hits | O-R7 (P1) |
| 5 | Second SM-2 engine sharing state | `spacedRepetitionScheduler.ts:93` | ❌ disjoint from `spacedRepetitionService.ts:177` | different table/columns, zero shared state | O-R3 (P1) |
| 6 | `writeMemory` typed import | exported `bookMemoryService.ts:510` | ⚠ reached only by runtime reflection guard | `memoryExtractionProcessor.ts:74-217` widened-type `typeof` | O-R4 (P2) |
| 7 | LLM guard middleware (`withGuards`) | `server/llm/middleware/index.ts:4` | ⚠ bypassed by GEPA/CE hot paths | grep: used by block-rewrite/book-validation, NOT `stitch.ts:65` / `llmJudgeCron.ts:259` | D-13 (P2) |
| 8 | `shouldDistillNow` policy knobs | `distill.ts:279` (`every_n_turns`, `on_token_pressure`) | ⚠ declared, all branches `return true` | `distill.ts:284` | D-3 (P2) |

8 candidates ≥ the 5-minimum. Items 1–5 are true zero-wire; 6–8 are
wired-but-defeated (reflection / bypass / no-op).

## Appendix B — Flag-mirroring table (chart source vs. `server/.env`)

Every named `FEATURE_*` / `BCE_*` / `OPENMEMORY_*` / `CONTEXT_*` flag, with a
present-in-chart verdict. Both lenses cross-checked; Codex confirms the ✅ rows
at the same anchors Opus uses.

| Flag | In chart? | Anchor | Risk |
|---|---|---|---|
| `FEATURE_PROMPT_EVOLUTION` | ✅ | `infra/charts/libwit/values.yaml:307` | OK |
| `BCE_M3_CALIBRATOR` | ✅ | `values.yaml:299` | OK |
| `BCE_M1_MEM0` | ✅ | sealed `infra/charts/libwit/templates/sealed/libwit-server-env.yaml:42` | OK |
| `OPENMEMORY_HOST` | ✅ | sealed `:145` | OK |
| `FEATURE_MEMORY_HARD_FAIL` | ❌ MISSING | read `memoryHealthProbe.ts:35` | **P1 — silent Mem0 failure (O-R5)** |
| `FEATURE_LLM_JUDGE_CRON` | ❌ MISSING | consumer `llmJudgeCron.ts:341` | **P0 — starves GEPA promotion (O-R1 ★ D-4)** |
| `CONTEXT_RECONCILE_CRON` | ❌ MISSING | `contextReconcile.ts` | P1 — no reconcile in prod (O-R6) |
| `CONTEXT_FSRS_DECAY_CRON` | ❌ MISSING | `contextFsrsDecay.ts` | P1 — memory monotonic growth (O-R6) |
| `CONTEXT_ENGINE_EVAL_INTERVAL_MS` | ❌ MISSING | `contextEngineEval.ts` | P2 — no eval loop (O-R6) |
| `FEATURE_LEGACY_GEPA_REFLECTOR` | ❌ MISSING | `gepaCron.ts:865` | P3 — default-OFF anyway |

**Caveat (read-only, both lenses):** "missing from chart" ≠ "empty in prod pod"
— flags can be injected via SealedSecret or `kubectl set env`; live pod env was
**not** inspected. The P0/P1 verdicts are chart-*source* verdicts. The fix
(mirror to chart) is correct regardless: an un-mirrored flag silently reverts on
the next `helm upgrade`.

---

*Generated by the synthesizer stage. Lens coverage 2/4 (codex, opus); glm+kimi
failed on missing API keys — re-run at full capacity per §7 before treating
correctness-class claims (esp. O-R3 SM-2 math drift) as verified.*
