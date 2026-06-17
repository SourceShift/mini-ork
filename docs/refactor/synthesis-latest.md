# Synthesis — highlight-viz / block-viz render-gate divergence

**Run:** `run-1781695664-41764`  
**Effective panel:** 2 substantive lenses (Kimi + Opus) + 1 thin Codex meta note + 1 missing GLM artifact. Quorum gate passed on file presence (3/4) but not on content depth.

---

## Section 1: Severity × leverage matrix

|          | HIGH leverage                                                                 | MED leverage                                          | LOW leverage |
|----------|-------------------------------------------------------------------------------|-------------------------------------------------------|--------------|
| **P1**   | K-01 ★ `isVizPlanPreview` hardening<br>K-02 ★ cross-block JSON reassembly<br>O-06 ★ Axis B primary recommendation | K-10 regression fixtures<br>O-04 hypothesis resolution<br>K-07 compact-JSON directive | K-09 prompt-registry cleanup |
| **P2**   | K-04 ★ segmenter JSON accumulation<br>K-05 JSON-node bypass<br>O-05 ★ suppress segmentation of preview-only research node<br>D-02 BAML cache consolidation | K-03 brace-balance segmenter guard<br>K-08 harden `parseVizPlanPreview`<br>D-03 cost cap / circuit breaker | K-06 synthetic parsed-JSON block emission |
| **P3**   | D-02 (deferred) unified highlight BAML class                                  | O-08 resolve Route-P vs Route-T race causally         | —            |

**Consensus legend:** ★ = finding appears in 2+ lenses.

---

## Section 2: Top 5 immediate wins (P1)

| ID | Title | Source lens | One-line fix | Effort |
|----|-------|-------------|--------------|--------|
| **K-01 ★** | Harden `isVizPlanPreview` against fences + preamble | Kimi refactor-1 / Opus O-06 | In `src/components/blocks/AnnotationPanelContent.tsx:263`, strip leading ` ```json ` fences and prose before `startsWith('{')`; locate first `{` instead of requiring block start. | 0.5 d |
| **K-02 ★** | Reassemble fragmented JSON across stream-tree markdown blocks | Kimi refactor-2 / Opus O-03,O-04 | In `src/components/blocks/AnnotationPanelContent.tsx:1146`, fall back to concatenating adjacent `markdown` blocks until a parseable `{..."sections"...}` emerges. | 1 d |
| **K-10** | Parameterized regression fixtures for all four hypotheses | Kimi refactor-10 | Add `VIZ_PLAN_FIXTURES` in `src/components/blocks/__tests__/AnnotationVizDialog.contract.test.ts` covering compact, pretty, fenced, and preamble-leading JSON. | 0.5 d |
| **O-05 ★** | Suppress markdown segmentation for the preview-only research node | Opus R5 / Kimi K-04,K-05 | Stop sending the research node's JSON through `StreamingMarkdownSegmenter`: either flip `emitUnderSeed` at `server/services/pipelineTemplates/highlightActions.ts:566` or route `outputParser:'json'` nodes around the segmenter at `server/services/pipelineExecutor.ts:1765`. | 1–2 d |
| **K-07** | Add compact-JSON directive to the viz research prompt | Kimi refactor-7 | Append "output the JSON object as a single compact line starting with `{`" to `baml_src/highlight_action.baml:240`. | 0.25 d |

**Total: ~3.25–4.25 engineering days.**

---

## Section 3: v0.x+1 architectural shifts (P2)

### 1. Data-layer / runtime — give JSON-output nodes a first-class contract
- **What:** Introduce `node.config.outputParser === 'json'` and bypass `StreamingMarkdownSegmenter` for those nodes. Emit one synthetic block via `block_open`/`block_chunk`/`block_close` at `server/services/pipelineExecutor.ts:1765` and `server/services/streamingMarkdownSegmenter.ts:489`.
- **Files:** `pipelineExecutor.ts:1765`, `streamingMarkdownSegmenter.ts:489`, `highlightActions.ts:566`.
- **Effort:** 2–3 wks.
- **Prerequisite P1s:** K-02, K-04, O-05.
- **Risk if deferred:** Every new JSON-emitting node re-creates this divergence; FE keeps accumulating defensive regexes for a BE structural problem.

### 2. LLM-dispatch — consolidate highlight-action BAML classes
- **What:** Merge `VizHighlight` / `AskHighlight` / `ExtendHighlight` / `ExplainHighlight` into one BAML class with a discriminated-union output, cutting cache namespaces from 8 (4 actions × 2 providers) to 2.
- **Files:** `baml_src/highlight_action.baml:211+`.
- **Effort:** 2–3 wks.
- **Prerequisite P1s:** stable detection (K-01, K-02).
- **Risk if deferred:** ~40–50% input-token waste on cross-action sessions; cache hit rate stays near 0%.

### 3. Observability / cost — cap the two-LLM serial path
- **What:** Add a per-run cost cap and retry circuit breaker for the Sonnet 4.6 fallback path (`$0.80 cap + 10 retry turns = $1.20 worst case`) and the unbounded 5-call session class.
- **Source:** Codex D-03.
- **Effort:** 1–2 wks.
- **Risk if deferred:** Pathological inputs burn $5+ per run silently.

---

## Section 4: Long-horizon (P3 + advisory)

- **P3-HIGH — Unified BAML highlight class (deferred from P2):** Do this only after detection is robust; otherwise prompt changes mask the real fix.
- **P3-MED — Causally resolve the Route-P vs Route-T race:** Opus §7 (`AnnotationPanelContent.tsx:1171-1195`) flags an unobserved timing dependency. Run the smoke recipe in §7 to determine whether `output_placement: page_after_selection` changes which render route is live at view time.
- **Advisory — Model-swap compliance guard:** DeepSeekV4Flash (the actual viz model per Codex D-01 at `baml_src/highlight_action.baml:211`) mostly obeys the bare-JSON system prompt, but no structural guard enforces it. Add a lightweight JSON-shape verifier before segmenter handoff.
- **Advisory — Prompt-eval harness fixtures:** Move K-10 into the project's prompt harness so prompt edits get regression-tested via `POST /api/harness/fixtures/from-execution/:executionId`.

---

## Section 5: Hardest open question

**Inherited from Opus lens §7:** Why does one path reach Route-T (post-segmentation block scan) while the other reaches Route-P (raw preview accumulator) at view time? The divergence depends on a race between (i) the segmenter's first `block_open`, (ii) the panel's first render, and (iii) the `streamTree → fetchedBlockTree` handoff gated at `src/components/blocks/AnnotationPanelContent.tsx:1171-1195`. `output_placement: {mode:'page_after_selection'}` plausibly shifts persistence timing, nudging block-viz toward one route and highlight-viz toward the other — but this is inference, not direct observation.

**Assessment of mitigations:** Axis B (tolerant FE detector) + Axis C (suppress segmentation of preview-only node) are sufficient *without* answering the race, because they make the boundary robust under either outcome. Axis B alone handles fences, preamble, and fragmentation; Axis C removes the fragile Route-T entirely. More research is useful only for prioritization: if smoke step 3 (see §7 below) shows exactly one well-formed `{`-leading block on both paths, the race is purely timing and Axis C alone is sufficient. If fragmentation is confirmed, both axes are warranted.

---

## Section 6: Dogfood reflection

This audit partially reproduced the `feature-inventory-cmgk` failure mode:

- **GLM lens:** missing entirely (`lens-glm.md` absent).
- **Codex lens:** only a 10-line verifier meta stub (`lens-codex.md` claimed 19.7 KB / 218 lines but contained no substantive findings).
- **Effective panel:** Kimi + Opus only.

The hard quorum gate in the recipe requires ≥3/4 non-empty artifacts; it passed on *presence* but not on *content quality*. The synthesizer therefore had to infer consensus from two substantive lenses rather than four. **Recommendation for the framework:** add a content-quality precondition (e.g., ≥N file:line citations per lens) before the synthesizer fires, and a per-lens fallback/retry when an artifact is below threshold.

No lens was blocked by a finding the audit itself identified — the block was upstream agent output quality, not a source-code obstacle.

---

## Section 7: How to re-run

```bash
# From repo root
cd /Volumes/docker-ssd/Migration/Development/researcher
git checkout main && git pull --ff-only

# Re-run this refactor-audit recipe
mini-ork run refactor-audit .mini-ork/kickoffs/20260617-viz-highlight-block-consolidation.md

# Verify quorum before synthesizer manually:
for f in lens-glm.md lens-kimi.md lens-codex.md lens-opus.md; do
  test -s "${MINI_ORK_RUN_DIR}/${f}" && echo "OK: $f" || echo "MISSING: $f"
done
```

**P1 that blocks self-dispatch:** none, but if `glm_lens` fails again the panel degrades to 2 lenses unless the fallback lane (currently `glm_lens → deepseek`) is wired in `.mini-ork/config/agents.yaml`.

**Falsifiable smoke recipe against `100.74.239.22:7825`:**

1. Trigger both entry points for the same document:
   - **highlight-viz:** no `output_placement`.
   - **block-viz:** `output_placement: {mode:'page_after_selection', selected_block_uuids}`.
   Both POST to `/api/pipeline-orchestrator/runs` with `pipeline_type: "viz_highlight"`.
2. **Loki check** (`100.74.239.22:13101`): `{app="libwit-backend"} |= "skipping viz research plan for page insertion"` — expect one line per run on both paths (confirms preview-only premise).
3. **Falsifier for fragmentation:** inspect the research node's `block_close` events. If any run yields >1 block for the JSON OR a block whose content does not start with `{`, hypothesis (b) is confirmed live.
4. **FE walk** (defer to user via `agent-browser`): mid-stream, assert `data-testid="annotation-dialog-viz-plan-stream"` renders on both paths and neither panel renders raw JSON in `annotation-dialog-streaming-tree`.

Pre-fix expectation: highlight-viz loses the plan card once `hasStreamTree` flips. Post-fix (Axis B + C): card present on both paths.
