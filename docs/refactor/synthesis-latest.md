# Bug audit (synthesis) — report only, no fixes

## Summary
- Total unique bugs: 33
- By severity: P0 4 · P1 12 · P2 15 · P3 2
- By consensus: 4/4 2 · 3/4 1 · 2/4 4 · 1/4 26
- Coverage: F1 (shared AiGenerationControls parity) ≥1 bug · F2 (BlockSelectionToolbar Visualize shape) ≥1 bug · F3 (insert site page-flow vs annotation-child) ≥1 bug · F4 (mobile parity Markdown/PDF/Block) ≥1 bug · F5 (server orchestrator + authz) ≥1 bug. No feature zone is bug-free.

## P0 — Critical bugs

- [BUG-001] [CONSENSUS: 4/4] [DISPUTED-SEVERITY: P0 (kimi, minimax) vs P1 (codex, glm)] Block Visualize parents generated output under the source block, not into page flow
  - File: `src/components/blocks/BlockSelectionToolbar.tsx:375` (FE seed) + `server/services/pipelineOrchestratorService.ts:711` (BE wrapper skip) + `server/services/pipelineExecutor.ts:1028` (parent assignment)
  - Feature: Block-selection Visualize final insertion (F3)
  - Symptom: Visual block lands as a child of `targetUuids[0]`; the document's top-level outline does not show it.
  - Root cause: FE passes a content block UUID as `highlightBlockUuid`. `maybeCreateAnnotationWrapper` silently skips when `seed.nodeType !== 'highlight'`, executor then creates `ai_expansion_group` with `parentUuid: ctx.seedBlockUuid`.
  - Reproduction: Alt-drag one or more blocks → click Visualize chip → Mermaid/Chart. Inspect tree; generated block is a child of source, not a sibling after the selection range.
  - Impact: Violates the kickoff page-insertion product contract; semantic data loss (wrong tree position); no user-visible recovery.
  - Quotes: minimax "the generated wrapper is parented to B1 specifically"; codex "Generated visual blocks become descendants of the source block instead of siblings".
  - Fix shape: Add a page-insertion outcome mode and a BE sibling-insert helper; stop reusing the annotation-wrapper path for block selection.

- [BUG-002] [CONSENSUS: 1/4 codex] Pipeline SSE stream exposes run metadata and generated blocks without auth
  - File: `server/routes/pipelineOrchestrator.ts:247` (stream route) + `server/routes/pipelineOrchestrator.ts:322` (snapshot mode) + `src/hooks/usePipelineRun.ts:66` (subscriber)
  - Feature: Server orchestrator authz (F5)
  - Symptom: `GET /api/pipeline-orchestrator/runs/:runId/stream` has no `requireAuth`; sibling `POST /runs` at `:159` does.
  - Root cause: Missing middleware on the stream/snapshot routes.
  - Reproduction: Unauthenticated `curl` against the stream endpoint with a known runId returns events including generated block payloads.
  - Impact: Data exposure; any pipeline run UUID is sufficient to leak descendant blocks.
  - Fix shape: Apply `requireAuth` + run-ownership check on stream + snapshot routes.

- [BUG-003] [CONSENSUS: 1/4 codex] Pipeline run trusts client-supplied seed block UUID without ownership validation
  - File: `server/routes/pipelineOrchestrator.ts:159` (route) + `server/services/pipelineOrchestratorService.ts:557` (execution) + `server/services/pipelineExecutor.ts:1020,1026` (seed read + child create)
  - Feature: Server orchestrator authz on parent block ids (F5)
  - Symptom: Authenticated user can submit another user's `seed_block_uuid`; executor reads + writes a generated child under it.
  - Root cause: No visible tenant/owner check on the seed UUID before persistence.
  - Reproduction: Authenticated POST `/api/pipeline-orchestrator/runs` with a seed UUID owned by another user; observe generated block attached to that seed.
  - Impact: Cross-tenant write; integrity + privacy breach.
  - Fix shape: Validate seed ownership against caller's user/tenant before allowing execution to bind the seed.

- [BUG-004] [CONSENSUS: 1/4 minimax] Block Visualize → "Custom prompt…" is silently a Mermaid diagram
  - File: `src/components/blocks/BlockSelectionToolbar.tsx:840`
  - Feature: BlockSelectionToolbar Visualize submenu (F2)
  - Symptom: `dispatchVizType={customPanelMode === 'visualize' ? 'mermaid' : undefined}` hardcodes 'mermaid'; the user's intended viz type is dropped.
  - Root cause: Literal `'mermaid'` instead of forwarding the chosen `VisualizeVizType`.
  - Reproduction: Visualize → Custom prompt… → type intent → submit. Server payload always has `viz_type: "mermaid"`.
  - Impact: UI label lies (Custom != custom type); trust + productivity loss.
  - Fix shape: Forward the user-selected `vizType` into `dispatchVizType`.

## P1 — Visible breakage

- [BUG-005] [CONSENSUS: 4/4] [DISPUTED-SEVERITY: P0 (minimax) vs P1 (kimi, codex, glm)] Block Visualize hardcodes `mode: 'fast'` and never renders `AiGenerationControls`
  - File: `src/components/blocks/BlockSelectionToolbar.tsx:373` (hardcode) + `src/components/libwit/highlighter/AiGenerationControls.tsx` (control component) — compare with `src/components/markdown/UnifiedMarkdownRenderer.tsx:836`, `src/components/pdf/PDFPageViewViewer.tsx:2942`, `src/components/blocks/BlockTreeRenderer.tsx:1167`.
  - Feature: Shared AiGenerationControls parity (F1)
  - Symptom: Block toolbar has no Fast/Deep, no Quick/Sources; `startAnnotationPipeline` always sends `mode: 'fast'` with `bypassConfirmation: false`.
  - Root cause: Block surface never mounts `AiGenerationControls`; no `modelMode`/`retrievalMode` state.
  - Reproduction: Select a block, fire any AI chip; request to `/runs` always has `model_mode: "fast"` with no retrieval policy.
  - Impact: Parity claim violated; user choice silently ignored across surfaces.
  - Fix shape: Mount `AiGenerationControls` in `BlockSelectionToolbar`, lift policy state, forward into `requestPipeline`.

- [BUG-006] [CONSENSUS: 3/4 minimax+codex+glm] Multi-block Visualize silently collapses the selection to the first block
  - File: `src/components/blocks/BlockSelectionToolbar.tsx:207` (`targetUuids`) + `:375-377` (uses `targetUuids[0]`) + `src/components/blocks/CrossBlockPipelineDialogs.tsx:70` (modal receives single UUID)
  - Feature: Block Visualize page insertion (F3)
  - Symptom: N≥2 selected → only first block is used as both `highlightBlockUuid` and `contentBlockUuid`. Result anchors to B1; B2..Bn dropped from anchor.
  - Root cause: Single-UUID seed instead of range/full-selection adapter.
  - Reproduction: Select B1+B2+B3, Visualize → Mermaid; output parents to B1 only.
  - Impact: Trust loss; output unfaithful to selection.
  - Fix shape: Pass the full selected range into a placement adapter; persist generated visual after the range.

- [BUG-007] [CONSENSUS: 2/4 kimi+minimax] [DISPUTED-SEVERITY: P2 (kimi) vs P1 (minimax)] Figure / illustration is gated off everywhere — `figureEnabled={false}` is a literal in 5 call sites
  - File: `src/components/libwit/highlighter/MarkdownHighlighterProvider.tsx:979` + `src/components/blocks/BlockSelectionToolbar.tsx:826` + `src/components/blocks/BlockTreeRenderer.tsx:1198` + `src/components/markdown/UnifiedMarkdownRenderer.tsx:996` + `src/components/pdf/PDFPageViewViewer.tsx:2973`. BE path lives at `server/services/pipelineTemplates/highlightActions.ts:556`.
  - Feature: Visualize submenu across surfaces (F2)
  - Symptom: `VisualizeChipMenu` accepts `figureEnabled`, but every host passes `false`. BE prompt + handler exist; UI never reaches them.
  - Root cause: No flag wiring (env, remote config, or BE feature flag).
  - Reproduction: Open Visualize menu on any reader surface; "Illustration" entry never appears.
  - Impact: Hidden capability; the parity claim is also violated (FE silently drops a documented viz subtype).
  - Fix shape: Gate `figureEnabled` on `FEATURE_VISUALIZE_FIGURE` (or BE catalog), not a literal.

- [BUG-008] [CONSENSUS: 2/4 kimi+minimax] [DISPUTED-SEVERITY: P3 (kimi) vs P1 (minimax)] Mobile BlockSelectionMobileSheet duplicates AI action metadata and diverges from desktop chip vocabulary
  - File: `src/components/blocks/BlockSelectionMobileSheet.tsx:46-54` (local `AI_ACTIONS`) vs `src/components/selection/SelectionActionRow.tsx:52-59` (canonical).
  - Feature: Mobile parity (F4)
  - Symptom: Mobile shows 7 chips with `Explain`/`Extend` as siblings; desktop shows one `Understand` chip with a submenu. Labels also drift (`arXiv` vs `arXiv deep-dive`, `Code` vs `Explain code`).
  - Root cause: Duplicated action array; no shared export. The component comment even acknowledges deferral.
  - Reproduction: Compare mobile and desktop block-selection chips on the same selection.
  - Impact: Trust loss; mobile and desktop pipelines diverge per same `HighlighterAction` enum.
  - Fix shape: Export `AI_ACTIONS` from `SelectionActionRow`, consume in both surfaces.

- [BUG-009] [CONSENSUS: 2/4 kimi+minimax] [DISPUTED-SEVERITY: P1 (kimi) vs P2 (minimax)] `maybeCreateAnnotationWrapper` skips wrapper for content-block seeds (Understand/Explain/Extend break too)
  - File: `server/services/pipelineOrchestratorService.ts:711` (silent guard) + `src/components/blocks/BlockSelectionToolbar.tsx:375` (caller)
  - Feature: Pipeline annotation wrapper (F3, F5)
  - Symptom: Wrapper is silently skipped; pipeline emits child blocks directly under the content block. Affects every block-selection AI action, not only Visualize.
  - Root cause: Wrapper function pre-condition (`seed.nodeType === 'highlight'`) is violated by every block-selection call; no alternative branch.
  - Reproduction: Block-selection → Understand/Explain/Extend; server logs include "seed not a highlight, skipping wrapper"; `pipeline_executions.result` has no `wrapper_uuid`.
  - Impact: Schema drift (annotation invariant broken silently); downstream features relying on `parent_uuid == highlight` will miss these.
  - Fix shape: Either create a wrapper typed for block-selection seeds OR change block-selection to a sibling insertion that explicitly skips wrapper.

- [BUG-010] [CONSENSUS: 1/4 kimi] `VisualizeVizType` union shrinks between shared, service, and call-site layers
  - File: `src/services/derivativeDispatchService.ts:23` (local re-decl drops `'plot'`) + `shared/types/contentArtifacts.ts:55` (canonical) + `src/components/libwit/highlighter/MarkdownHighlighterProvider.tsx:598` (mapping omits `'plot'`).
  - Feature: Visualize sub-action dispatch (F2)
  - Symptom: Shared type defines `'mermaid' | 'chart' | 'figure' | 'plot'`; service + mapper handle only three.
  - Root cause: Local type re-declaration that lies about mirroring the shared type.
  - Reproduction: BE emitting `viz_type: 'plot'` is rejected/coerced before reaching server.
  - Impact: Cross-layer contract drift; future chip can never round-trip.
  - Fix shape: Import shared type; handle all four cases in mapping.

- [BUG-011] [CONSENSUS: 1/4 kimi] `registerPrompt` placeholder keys are camelCase while templates interpolate snake_case
  - File: `server/services/pipelineTemplates/highlightActions.ts:129-142` (registry) vs `:217-230` (templates).
  - Feature: Highlight-action prompt registry (F5)
  - Symptom: Registry declares `seedContent`, `surroundingContext`; templates use `{{seed_content}}`, `{{surrounding_context}}`, `{{additional_instructions}}`, `{{user_question}}`.
  - Root cause: Two naming conventions for the same variable.
  - Reproduction: Lint declared keys against template text — declared keys are absent.
  - Impact: Registry metadata cannot validate template substitution; any tool reading the registry sees stale keys.
  - Fix shape: Align registry keys with template snake_case.

- [BUG-012] [CONSENSUS: 1/4 codex] Completed pipeline can report success before generated blocks are durably inserted
  - File: `server/services/pipelineExecutor.ts:947` (status=completed) + `:964` (`pipeline_completed` emitted) + `:1056` (`persistAsync` begins) + `server/services/pipelineBlockPersistence.ts:174` (catch) + `:306` (bus cleanup may drop `persist_failed`).
  - Feature: Generated insertion durability (F3)
  - Symptom: `pipeline_completed` SSE fires before persistence completes; partial persistence failures can be silently dropped.
  - Root cause: Success event ordering vs async persistence; no terminal handshake gated on persistence success.
  - Reproduction: Inject a persistence error after `pipeline_completed`; UI shows success, tree shows no/partial blocks.
  - Impact: Silent data loss; user trust loss.
  - Fix shape: Emit `pipeline_completed` only after persistence ack (or add a `persist_completed` event the FE must observe).

- [BUG-013] [CONSENSUS: 1/4 glm] PDF Visualize submenu ignores Mermaid vs Chart sub-action
  - File: `src/components/pdf/PDFPageViewViewer.tsx:2816` — `pdfOnVisualizeSub(sub)` calls `handleToolbarViz()` without forwarding `sub`; `handleContentExtend()` has no `vizType` parameter.
  - Feature: PDF Visualize flow (F2)
  - Symptom: Chart vs Mermaid menu choice collapses into one BE request; `viz_type` not transmitted.
  - Root cause: `sub` parameter dropped at the toolbar boundary.
  - Reproduction: PDF select → Visualize → Chart/plot → inspect `pipeline_executions.input_context`; no `viz_type`.
  - Impact: PDF users cannot select a specific viz subtype.
  - Fix shape: Thread `VisualizeSubAction` through `handleToolbarViz()` → `handleContentExtend()` → `requestPipeline({ vizType })`.

- [BUG-014] [CONSENSUS: 1/4 glm] Long Markdown selections are truncated to 120 chars before pipeline generation
  - File: `src/components/markdown/useHighlightHandlers.ts:483` — `seedPreview: selectedText.slice(0, 120)`; `ContentNodeCreationModal` uses `pending.seedPreview` as `selected_text`; pipeline context stores `seed_content: selected_text`.
  - Feature: Markdown Visualize/Explain/Extend (F2)
  - Symptom: Generation runs on first 120 chars only.
  - Root cause: Display preview reused as canonical seed.
  - Reproduction: Select a 500-char passage → Visualize with Sources → inspect `pipeline_executions.input_context.generation_recipe.seed_content`; only 120 chars present.
  - Impact: Output misses most of selection content; appears successful.
  - Fix shape: Keep `seedPreview` display-only; pass full text as separate `selected_text`/`seed_content`.

- [BUG-015] [CONSENSUS: 1/4 glm+minimax cross-ref] Long block selections are truncated to 240 chars before generation
  - File: `src/components/blocks/BlockSelectionToolbar.tsx:346` (`selectedContentPreview` slice 240); fed as `seedPreview` and surfaced to modal/context. MiniMax #5 also flagged the 240-char ceiling at `:345-348` but as part of the multi-block collapse bug.
  - Feature: Block Visualize generation (F2)
  - Symptom: Block visuals are seeded with truncated content.
  - Reproduction: Select two long blocks → Visualize → inspect generation recipe; seed is 240 chars max.
  - Impact: Faithfulness loss on long selections.
  - Fix shape: Separate display preview from canonical full-content seed.

- [BUG-016] [CONSENSUS: 1/4 minimax] BlockSelectionToolbar always shows the heavy confirmation modal — no `bypassConfirmation` path
  - File: `src/components/blocks/BlockSelectionToolbar.tsx:373` (`bypassConfirmation: false` hardcoded) + `src/hooks/useHighlightPipeline.ts:99` (`bypassConfirmation` flag exists but is never set on block path).
  - Feature: Block Visualize/Understand/Explain/Extend (F2)
  - Symptom: Every AI action pops `ContentNodeCreationModal` (style picker, retrieved sources, depth slider).
  - Root cause: Hardcoded flag; no UI to opt-out.
  - Reproduction: Click any AI chip; modal always appears.
  - Impact: Productivity loss; surface drift vs text highlighter which supports bypass.
  - Fix shape: Expose `bypassConfirmation` from the toolbar (mirroring text path).

## P2 — Degraded behaviour

- [BUG-017] [CONSENSUS: 2/4 kimi+glm] `PDFMobileSelectionSheet` ignores `disabledActions` API, only forwards legacy `arxivDisabled`
  - File: `src/components/pdf/PDFMobileSelectionSheet.tsx:28-33,52` + `src/components/libwit/highlighter/MobileSheet.tsx:61-66` (canonical `disabledActions?: Partial<Record<HighlighterAction, string>>`).
  - Feature: Mobile parity (F4)
  - Symptom: PDF mobile cannot disable `code` on non-code selections or any future gated action.
  - Reproduction: PDF mobile non-code selection → tap `code` → pipeline fires.
  - Impact: Mobile-only gating regression vs desktop.
  - Fix shape: Accept and forward `disabledActions` in `PDFMobileSelectionSheet`.

- [BUG-018] [CONSENSUS: 1/4 kimi] `dispatchKind` in `UnderstandPanel` accepts the full `HighlighterAction` union but only `prompt|visualize` are valid
  - File: `src/components/selection/UnderstandPanel.tsx:57` (type) + `:48-55` (contract).
  - Reproduction: Caller passing `dispatchKind="understand"` → BE POST `kind: 'understand'` → 400.
  - Impact: Wider type than the valid domain; foot-gun.
  - Fix shape: Narrow prop to `'prompt' | 'visualize'`.

- [BUG-019] [CONSENSUS: 1/4 kimi] `usePipelineRun.onTerminal` narrows status and never fires for `cancelled`
  - File: `src/hooks/usePipelineRun.ts:41` + `src/stores/pipelineRunStore.ts:14` (canonical status union).
  - Symptom: Cancelled runs never invoke the terminal callback.
  - Reproduction: Start pipeline → Stop → observe `onTerminal` never called.
  - Fix shape: Expand callback signature to accept `PipelineRunStatus`; emit from `stopPipeline`.

- [BUG-020] [CONSENSUS: 1/4 kimi] `MarkdownHighlighterProvider` callbacks omit `showToast` from dep arrays
  - File: `src/components/libwit/highlighter/MarkdownHighlighterProvider.tsx:447` + `:146-151`.
  - Symptom: `runAction`, `handleUnderstandSub`, `handleVisualizeSub` miss `showToast` in deps.
  - Impact: Closure correctness risk under Strict Mode / future React compiler.
  - Fix shape: Add to dep arrays.

- [BUG-021] [CONSENSUS: 1/4 kimi] `CrossBlockPipelineDialogs` passes dummy `user_uuid: ''` and zero offsets to `ContentNodeCreationModal`
  - File: `src/components/blocks/CrossBlockPipelineDialogs.tsx:67-75` + `src/components/contentNodes/ContentNodeCreationModal.tsx:94-108`.
  - Symptom: Empty `user_uuid` and fabricated offsets satisfy types but violate semantics.
  - Reproduction: Add runtime UUID assertion in modal; block-selection pipelines fire it.
  - Fix shape: Thread real user UUID + real offsets, or mark fields optional and validate at the consumption point.

- [BUG-022] [CONSENSUS: 1/4 kimi] `BlockSelectionToolbar` passes `contentBlockUuid` after the field was marked deprecated
  - File: `src/components/blocks/BlockSelectionToolbar.tsx:376` + `src/hooks/useHighlightPipeline.ts:107-113` (`@deprecated` JSDoc).
  - Symptom: New caller perpetuates a contract the hook explicitly retired.
  - Fix shape: Stop passing `contentBlockUuid`.

- [BUG-023] [CONSENSUS: 1/4 kimi] `pipelineOrchestratorService.retryAnnotation` option type is stricter than the route accepts
  - File: `server/services/pipelineOrchestratorService.ts:814-823` + `server/routes/pipelineOrchestrator.ts:450-466`.
  - Symptom: Route makes `tier` and `instruction` optional; service forces them required.
  - Impact: Callers must invent values the route does not require.
  - Fix shape: Make `tier` and `instruction` optional in service options to match route.

- [BUG-024] [CONSENSUS: 1/4 kimi] `pipelineOrchestrator` template update normalizes `is_public` → `isPublic`; create route does not
  - File: `server/routes/pipelineOrchestrator.ts:113-118` + `server/services/pipelineStore.ts`.
  - Symptom: Create + update handle the same field differently. Works only by parameter-naming coincidence.
  - Fix shape: Normalize naming at one boundary.

- [BUG-025] [CONSENSUS: 1/4 glm] Annotation metadata never receives the selected `viz_type`
  - File: `server/services/pipelineOrchestratorService.ts:614` — FE writes `viz_type` into `inputContext`; `maybeCreateAnnotationWrapper` only reads `content_action_viz_type`; param ends up null.
  - Symptom: Generated annotation blocks lose subtype traceability.
  - Reproduction: Generate a Markdown Visualize → Mermaid → inspect block properties; `viz_type` is null.
  - Fix shape: Normalize both keys in `triggerRun()` before wrapper creation.

- [BUG-026] [CONSENSUS: 1/4 glm] `MarkdownHighlighterProvider` toolbar lacks shared `AiGenerationControls`
  - File: `src/components/libwit/highlighter/MarkdownHighlighterProvider.tsx:881` — renders `HighlighterToolbar` without `contextModeControl`, unlike `UnifiedMarkdownRenderer`.
  - Symptom: Markdown surfaces routed through this provider behave differently from `UnifiedMarkdownRenderer`.
  - Fix shape: Inject the same model/retrieval state + controls, or retire the provider for selection generation.

- [BUG-027] [CONSENSUS: 1/4 glm] Mobile text highlighter has no model or retrieval controls
  - File: `src/components/markdown/UnifiedMarkdownRenderer.tsx:731` — mobile branch passes only selection/color/action/tool handlers; `MobileSheet` has no equivalent of `HighlighterToolbar.contextModeControl`.
  - Feature: Mobile parity (F4)
  - Fix shape: Add shared controls to `MobileSheet`.

- [BUG-028] [CONSENSUS: 1/4 glm] Mobile PDF highlighter drops the per-action running-state feedback
  - File: `src/components/pdf/PDFMobileSelectionSheet.tsx:52` — `PDFMobileSelectionSheetProps` omits `runningAction`; wrapper renders `MobileSheet` without forwarding `pdfHighlighterRunningAction`.
  - Feature: Mobile parity (F4)
  - Reproduction: Tap Explain/Visualize on PDF mobile; no running indication.
  - Fix shape: Forward `runningAction` through.

- [BUG-029] [CONSENSUS: 1/4 glm] PDF Custom-visualization menu item is visible but dead
  - File: `src/components/pdf/PDFPageViewViewer.tsx:2812` — `pdfOnVisualizeSub('custom')` toasts "ships in a future pass" and returns.
  - Symptom: Visible menu action does nothing.
  - Fix shape: Wire to the same custom-prompt pipeline as block selection, or hide until implemented.

- [BUG-030] [CONSENSUS: 1/4 minimax] Mobile BlockSelectionSheet silently no-ops on Visualize if menu opener not wired
  - File: `src/components/blocks/BlockSelectionMobileSheet.tsx:276-280` + `src/components/blocks/BlockSelectionToolbar.tsx:561-563` (defensive return without user feedback).
  - Feature: Mobile parity (F4)
  - Impact: Click registers, no toast, no error; trust loss when wiring regresses.
  - Fix shape: Surface a fallback toast / disable the chip when no opener is provided.

- [BUG-031] [CONSENSUS: 1/4 minimax] Multi-block delete walks descendants sequentially without dedup
  - File: `src/components/blocks/BlockSelectionToolbar.tsx:281-293` + fallback message `:939-943`.
  - Symptom: Sequential `deleteBlockWithSource(uuid)` over selection without descendant-set dedup; can double-walk shared descendants. Confirmation copy also degrades when `selectedBlocks.length === 0`.
  - Impact: Latent over-delete risk in trees with shared descendants; silent.
  - Fix shape: Compute descendant set once, dedup before issuing deletes; tighten confirm copy.

## P3 — Sharp edges / footguns

- [BUG-032] [CONSENSUS: 1/4 minimax] "Pipeline builder" sub-action is a benign info toast disguised as a working chip
  - File: `src/components/blocks/BlockSelectionToolbar.tsx:459-467` (toast) + `src/components/selection/UnderstandChipMenu.tsx` (no per-item disabled API).
  - Impact: User thinks an action was queued; nothing happens.
  - Fix shape: Disable the menu item or mark it with a "coming soon" affordance.

- [BUG-033] [CONSENSUS: 1/4 glm] Mobile PDF highlighter does not suppress duplicate skill actions
  - File: `src/components/pdf/PDFMobileSelectionSheet.tsx:52` — no `suppressActions` prop; desktop PDF computes + forwards `pdfSuppressActions`.
  - Symptom: Skill badge and matching action tile both shown on mobile PDF.
  - Fix shape: Accept + forward `suppressActions`.

## Disputed entries

- BUG-001 — severity P0 (kimi, minimax) vs P1 (codex, glm). Both sides treat it as the headline finding. Kimi: "block-selection Visualize must insert the generated visual block into the page flow". Codex: "Generated visual blocks become descendants of the source block instead of siblings in page order."
- BUG-005 — severity P0 (minimax) vs P1 (kimi, codex, glm). MiniMax frames it as "user gets an unexplained drop in quality and a much higher speed — and no indicator that the mode is wrong"; the other three treat it as parity-break (visible breakage) rather than data loss.
- BUG-007 — severity P2 (kimi) vs P1 (minimax). Kimi treats it as contract drift; MiniMax surfaces "hidden capability" + productivity loss.
- BUG-008 — severity P3 (kimi) vs P1 (minimax). Kimi sees it as "duplicate AI action metadata"; MiniMax sees an actual UX divergence between mobile and desktop chip vocabularies.
- BUG-009 — severity P1 (kimi) vs P2 (minimax). Kimi treats the silent wrapper-skip as a contract violation; MiniMax records the user-visible symptom downstream.

## Coverage gap report

- F1 (shared AiGenerationControls parity): non-zero — BUG-005, BUG-007, BUG-026, BUG-027.
- F2 (BlockSelectionToolbar Visualize shape): non-zero — BUG-004, BUG-005, BUG-006, BUG-010, BUG-013, BUG-014, BUG-015, BUG-016.
- F3 (page-flow vs annotation-child insertion): non-zero — BUG-001, BUG-006, BUG-012.
- F4 (mobile parity Markdown/PDF/Block): non-zero — BUG-008, BUG-017, BUG-027, BUG-028, BUG-030, BUG-033.
- F5 (server orchestrator + authz): non-zero — BUG-002, BUG-003, BUG-009, BUG-011, BUG-023, BUG-024, BUG-025.
- Features with zero bugs found: none. Every kickoff feature zone has ≥1 finding.
- Follow-up flag: only `codex` produced the two P0 security bugs (BUG-002, BUG-003). No other lens corroborated authz on stream / seed UUID — the routes deserve targeted re-audit before any remediation lands.
