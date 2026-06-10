# Feature inventory (synthesis)

## Summary
- Total unique features: 412
- Consensus 4/4: 0
- Consensus 3/4: 18
- Consensus 2/4: 96
- Single-lens finds: 298

## Routes / endpoints (118 features)

### Prompt Evolution (GEPA) — admin surface
- `GET /fleet-overview` — `server/routes/promptEvolution.ts:69` — Fleet dashboard of all prompt keys with mutation streaks/fitness/shadow-test status. [CONSENSUS: 2/4] [STATUS: shipped] (GLM + MiniMax via `GepaDashboardPage`)
- `GET /judge-rubrics` — `server/routes/promptEvolution.ts:93` — List registered judge rubrics for GEPA-classified prompt keys. [CONSENSUS: 1/4] [STATUS: shipped] (GLM)
- `PATCH /judge-rubrics/:prompt_key` — `server/routes/promptEvolution.ts:116` — Update rubric weights/thresholds per prompt key. [CONSENSUS: 1/4] [STATUS: shipped] (GLM)
- `GET /eligible` — `server/routes/promptEvolution.ts:173` — Returns prompt keys eligible for GEPA mutation cycles. [CONSENSUS: 1/4] [STATUS: shipped] (GLM)
- `GET /:slug/streak` — `server/routes/promptEvolution.ts:191` — Consecutive-run fitness streak per slug. [CONSENSUS: 1/4] [STATUS: shipped] (GLM)
- `GET /:slug/fitness` — `server/routes/promptEvolution.ts:208` — Time-series fitness scores per slug. [CONSENSUS: 1/4] [STATUS: shipped] (GLM)
- `GET /:slug/mutations` — `server/routes/promptEvolution.ts:228` — Mutation history with verdicts (approved/rejected/pending). [CONSENSUS: 1/4] [STATUS: shipped] (GLM)
- `POST /:slug/pause` — `server/routes/promptEvolution.ts:265` — Pause GEPA evolution for a slug. [CONSENSUS: 2/4] [STATUS: shipped] (GLM + MiniMax GePauseModal)
- `POST /:slug/resume` — `server/routes/promptEvolution.ts:289` — Resume paused GEPA evolution. [CONSENSUS: 2/4] [STATUS: shipped] (GLM + MiniMax GeResumeModal)
- `POST /runs/:runId/rollback` — `server/routes/promptEvolution.ts:318` — Rollback GEPA run to prior prompt version. [CONSENSUS: 1/4] [STATUS: shipped] (GLM)
- `POST /mutations/:mutationUuid/approve` — `server/routes/promptEvolution.ts:350` — HITL approve a pending mutation. [CONSENSUS: 1/4] [STATUS: shipped] (GLM)
- `POST /mutations/:mutationUuid/reject` — `server/routes/promptEvolution.ts:382` — HITL reject a pending mutation. [CONSENSUS: 1/4] [STATUS: shipped] (GLM)

### Unified Prompt Feedback
- `POST /feedback` — `server/routes/promptFeedback.ts:32` — Unified feedback ingestion (chat thumbs, explanation thumbs, block rewrite preview, generic LLM-output) routed through `withFeature({name:'prompt-feedback'})`. [CONSENSUS: 3/4] [STATUS: shipped] (GLM + Codex chat/explanation/rewrite flows + MiniMax FeedbackThumbs)
- Validates source-specific payloads — `server/routes/promptFeedback.ts:55`
- Persists via `unifiedPromptFeedbackService.ts:94` (chat), `:148` (block_rewrite), `:196` (explanation), with prompt_execution acceptance backfill `:185/:232`

### Book Generation
- `POST /draft` — `server/routes/bookGeneration.ts:389` — Create book generation draft job. [CONSENSUS: 2/4] [STATUS: shipped] [OBS: missing route-level withFeature] (GLM + Codex)
- `PATCH /job/:jobId/draft` — `server/routes/bookGeneration.ts:429` — Update draft metadata. [CONSENSUS: 1/4] (GLM)
- `GET /job/:jobId/draft` — `server/routes/bookGeneration.ts:468` — Retrieve draft state. [CONSENSUS: 1/4] (GLM)
- `POST /confirm-intent/:jobId` — `server/routes/bookGeneration.ts:499` — Confirm intent before plan generation. [CONSENSUS: 1/4] (GLM)
- `POST /generate-style-guide` — `server/routes/bookGeneration.ts:577` — Generate writing style guide. [CONSENSUS: 1/4] (GLM)
- `GET /writing-styles` — `server/routes/bookGeneration.ts:640` — List saved writing styles. [CONSENSUS: 1/4] (GLM)
- `POST /writing-styles` — `server/routes/bookGeneration.ts:680` — Save new writing style. [CONSENSUS: 1/4] (GLM)
- `DELETE /writing-styles/:styleUuid` — `server/routes/bookGeneration.ts:724` — Delete saved style. [CONSENSUS: 1/4] (GLM)
- `POST /plan-sketch` — `server/routes/bookGeneration.ts:763` — Lightweight outline. [CONSENSUS: 1/4] (GLM)
- `POST /generate-plan` — `server/routes/bookGeneration.ts:817` — Full plan with evidence cascade; lifecycle wraps via `withFeature({name:'book-generation'})` (`lifecycle.ts:659`); routes through Hatchet or BullMQ fallback (`bookGenerationQueueService.ts:371/417`). [CONSENSUS: 2/4] [STATUS: shipped] (GLM + Codex)
- `POST /job/:jobId/confirm` — `server/routes/bookGeneration.ts:955` — Confirm plan + start chapter fanout via Hatchet `bookSetupProcessor:48/105` → `chapterGenerationProcessor:45/138/144`. [CONSENSUS: 3/4] [STATUS: shipped] (GLM + Codex + Kimi `:312` confirms via `validatePlanScript` + `jobActionRateLimit`)
- `POST /start-job` — `server/routes/bookGeneration.ts:1014/1029` — Alternative start with optional context distillation scheduled (`:1054`). [CONSENSUS: 2/4] (GLM + Codex)
- `POST /job/:jobId/retry` — `server/routes/bookGeneration.ts:1111` — Retry failed job. [CONSENSUS: 1/4] (GLM)
- `POST /job/:jobId/resume` — `server/routes/bookGeneration.ts:1196` — Resume paused/stuck from last checkpoint. [CONSENSUS: 1/4] (GLM) [DISPUTED: MiniMax claims "No resume-from-checkpoint for chapter generation" exists — GLM lists this route but MiniMax §"Coverage gap report" #1 says it has no `currentChapter` re-entry or partial-merge logic; route exists but semantics may be admin-only]
- `GET /job/:jobId/write-cockpit` — `server/routes/bookGeneration.ts:1428` — Live chapter-gen progress with per-chapter status. [CONSENSUS: 1/4] (GLM)
- `GET /job/:jobId` — `server/routes/bookGeneration.ts:1466` — Full job status. [CONSENSUS: 1/4] (GLM)
- `GET /by-document/:documentUUID` — `server/routes/bookGeneration.ts:1645` — Find job by document UUID. [CONSENSUS: 1/4] (GLM)
- `POST /job/:jobId/plan/steer` — `server/routes/bookGeneration.ts:2147` — Plan-level steer event; enqueues interrupt marker `:2158` + persists `:2170`. [CONSENSUS: 2/4] [OBS: missing — no withFeature/traceGemini] (GLM + Codex)
- `POST /job/:jobId/chapter/:chapterNumber/steer` — `server/routes/bookGeneration.ts:2235/2247` — Chapter-level steer event; persists `:2255`; UI polls queue state `:2293`. [CONSENSUS: 2/4] [OBS: missing] (GLM + Codex)
- `POST /job/:jobId/force-resume` — `server/routes/bookGeneration.ts:2321` — Force-resume bypassing guards (admin/debug). [CONSENSUS: 1/4] (GLM)
- `POST /job/:jobId/hatchet-cancel` — `server/routes/bookGeneration.ts:2346` — Cancel Hatchet workflow (admin/debug). [CONSENSUS: 1/4] (GLM) [DISPUTED: MiniMax §"Coverage gap" #2 says "no admin-side kill switch"; GLM lists this debug route — gap is FE/admin-UI surface, not backend route]
- `POST /job/:jobId/hatchet-replay` — `server/routes/bookGeneration.ts:2368` — Replay Hatchet from failed step. [CONSENSUS: 1/4] (GLM)
- `POST /job/:jobId/replan-orphans` — `server/routes/bookGeneration.ts:2560` — Replan failed/orphaned chapters. [CONSENSUS: 1/4] (GLM)
- `POST /job/:jobId/repair-import` — `server/routes/bookGeneration.ts:2697` — Repair import-source chapter data. [CONSENSUS: 1/4] (GLM)
- `POST /job/:jobId/build-appendixes` — `server/routes/bookGeneration.ts:2725` — Build glossary/cross-refs/index. [CONSENSUS: 1/4] (GLM)
- `DELETE /job/:jobId` — `server/routes/bookGeneration.ts:2791` — Delete job + data. [CONSENSUS: 1/4] (GLM)
- `POST /job/:jobId/regenerate-chapter` — `server/routes/bookGeneration.ts:2856` — Regenerate single chapter. [CONSENSUS: 1/4] (GLM)
- `POST /plan-from-sandbox` — `server/routes/bookGeneration.ts:3094` — Generate plan from sandbox agent output. [CONSENSUS: 1/4] (GLM)
- `POST /upload-from-sandbox` — `server/routes/bookGeneration.ts:3177` — Upload chapter content from sandbox. [CONSENSUS: 1/4] (GLM)
- `GET /search-chapters/:bookUUID` — `server/routes/bookGeneration.ts:3386` — Search within book chapters. [CONSENSUS: 1/4] (GLM)

### Books (reader-facing)
- `GET /` — `server/routes/books.ts:32` — List user's books. [CONSENSUS: 1/4] (GLM)
- `GET /:bookUUID/chapters` — `server/routes/books.ts:114` — List chapters with metadata. [CONSENSUS: 1/4] (GLM)
- `GET /:bookUUID/chapters/:chapterNumber/markdown` — `server/routes/books.ts:210` — Rendered chapter markdown. [CONSENSUS: 1/4] (GLM)
- `POST /:bookUUID/chapters/:chapterNumber/regenerate` — `server/routes/books.ts:265` — Regenerate chapter from reader. [CONSENSUS: 1/4] (GLM)

### Blocks (core content)
- `GET /by-source/:sourceType/:sourceId` — `server/routes/blocks.ts:77` — Fetch blocks by source ref; workspace-scoped ownership via `checkOwnership`/`requireWorkspaceContext`. [CONSENSUS: 2/4] [STATUS: shipped, IDOR-tested] (GLM + Kimi contract)
- `GET /by-document/:documentUuid/type/:nodeType` — `server/routes/blocks.ts:125` — Typed block retrieval. [CONSENSUS: 1/4] (GLM)
- `GET /:blockUuid/lineage` — `server/routes/blocks.ts:523` — Block provenance chain. [CONSENSUS: 1/4] (GLM)
- `PUT /:blockUuid` — `server/routes/blocks.ts:543` — Update block content. [CONSENSUS: 1/4] (GLM)
- `POST /` — `server/routes/blocks.ts:617` — Create new block. [CONSENSUS: 1/4] (GLM)
- `POST /migrate/chapters/decompose` — `server/routes/blocks.ts:987` — Decompose chapter into Logseq blocks. [CONSENSUS: 1/4] (GLM)

### Highlights
- `POST /` — `server/routes/highlights.ts:154` — Create highlight. [CONSENSUS: 1/4] (GLM)
- `POST /unified` — `server/routes/highlights.ts:227` — Create highlight with cross-ref linking. [CONSENSUS: 1/4] (GLM)
- `GET /` — `server/routes/highlights.ts:298` — List highlights for document. [CONSENSUS: 1/4] (GLM)
- `PATCH /:highlightUUID/note` — `server/routes/highlights.ts:444` — Update highlight note. [CONSENSUS: 1/4] (GLM)

### Chat Sessions
- `POST /` — `server/routes/chatSessions.ts:77` — Create new chat session. [CONSENSUS: 1/4] (GLM)
- `POST /:uuid/stream` — `server/routes/chatSessions.ts:887` — SSE streaming chat. [CONSENSUS: 1/4] (GLM)
- `GET /:uuid/messages` — `server/routes/chatSessions.ts:544` — Fetch session messages. [CONSENSUS: 1/4] (GLM)
- `POST /:uuid/message` — `server/routes/chatSessions.ts:831` — Send non-streaming message. [CONSENSUS: 1/4] (GLM)
- `GET /search` — `server/routes/chatSessions.ts:277` — Cross-session search. [CONSENSUS: 1/4] (GLM)

### Markdown Collections (documents)
- `POST /` — `server/routes/markdownCollections.ts:101` — Create document from markdown. [CONSENSUS: 1/4] (GLM)
- `GET /:documentUUID` — `server/routes/markdownCollections.ts:146` — Document metadata. [CONSENSUS: 1/4] (GLM)
- `GET /:documentUUID/files/:fileUUID` — `server/routes/markdownCollections.ts:197` — File content; `COALESCE(NULLIF(b.content,''), b.properties->>'markdown_content')`. [CONSENSUS: 1/4] (GLM)
- `GET /:documentUUID/search` — `server/routes/markdownCollections.ts:392` — Search within document. [CONSENSUS: 1/4] (GLM)
- `GET /:documentUUID/combined-content` — `server/routes/markdownCollections.ts:460` — Combined content for multi-file docs. [CONSENSUS: 1/4] (GLM)

### Document Upload
- `POST /upload` — `server/routes/documentUpload.ts:34` + Kimi `documentUploadPipeline.validateAndStore` at `documentUploadPipeline.ts:94` — Upload with SHA-256 dedup via `processed_content_cache`; arXiv URL→PDF rewrite; modes `fresh|resume|copy_from_cache`. [CONSENSUS: 2/4] (GLM + Kimi)
- `POST /upload-from-url` — `server/routes/documentUpload.ts:111` — URL import. [CONSENSUS: 1/4] (GLM)

### Book Translation
- `POST /books/:uuid/translate` — `server/routes/bookTranslation.ts:243` — Start full book translation through `bookTranslationProcessor` with `<<<PRESERVE_n>>>` token wrap. [CONSENSUS: 2/4] (GLM + MiniMax)
- `GET /books/:uuid/language` — `server/routes/bookTranslation.ts:343` — Book language (RTL/LTR fallback). [CONSENSUS: 1/4] (GLM)

### Knowledge Graph
- `GET /node/:topic` — `server/routes/knowledgeGraph.ts:187` — Get KG node by topic. [CONSENSUS: 1/4] (GLM)
- `POST /expand/:topic` — `server/routes/knowledgeGraph.ts:639` — Expand KG node. [CONSENSUS: 1/4] (GLM)
- `GET /search` — `server/routes/knowledgeGraph.ts:997` — Search KG nodes. [CONSENSUS: 1/4] (GLM)
- `GET /learning-path` — `server/routes/knowledgeGraph.ts:1052` — Learning path between topics. [CONSENSUS: 1/4] (GLM)
- `POST /from-highlight` — `server/routes/knowledgeGraph.ts:1238` — Create KG node from highlight. [CONSENSUS: 1/4] (GLM)

### Publisher Style
- `POST /generate` (style synthesis) — `server/routes/publisherStyleSynthesis.ts:35` — Synthesize publisher style; persists with `calibration_bucket`. [CONSENSUS: 2/4] (GLM + MiniMax StepStyleSynthesis)

### Onboarding Sample Chapter
- `POST /:sessionId/generate-variants` — `server/routes/sampleChapterHarvest.ts:223/237` — Dispatch sample variants via `dispatchSampleVariants` → ensures `book_generation_runs` row (`variantDispatch.ts:110/159`) → enqueues Hatchet task (`sampleVariantEnqueue.ts:42/74`) → persistence to `style_blueprint_samples` (`samplePersistence.ts:39`). [CONSENSUS: 2/4] [OBS: missing route-level withFeature] (Codex + MiniMax)
- `POST /:sessionId/section-feedback` — `server/routes/sampleChapterHarvest.ts:386/414` — Section feedback persisted to `blueprint_section_feedback` via `samplePersistence.ts:134/150`. [CONSENSUS: 1/4] [OBS: missing] (Codex)
- `GET /:sessionId/fetch` — `server/routes/sampleChapterHarvest.ts:447` — Reload session with samples/feedback/rubrics via `sessionStore.ts:137/145/168/187/206`. [CONSENSUS: 1/4] [OBS: OTel span `sampleChapterHarvest.sessionStore.load`] (Codex)
- `server/routes/onboardingSampleChapter.ts:57-137` — Registers 5 prompts (system/user/intent-classifier/clarifying-questions/plan-draft) for sample chapter wizard. [CONSENSUS: 1/4] (GLM)

### Podcasts / Audiobooks
- `POST /generate` (podcasts) — `server/routes/podcasts.ts:90` — Generate podcast from document. [CONSENSUS: 2/4] (GLM + MiniMax PodcastPanel)
- `POST /prepare` (audiobooks) — `server/routes/audiobooks.ts:44` — Prepare audiobook TTS. [CONSENSUS: 2/4] (GLM + MiniMax AudiobookPanel)

### Reading Progress
- `GET /:documentUUID` — `server/routes/readingProgress.ts:50` — Get progress items. [CONSENSUS: 1/4] (GLM)
- `POST /:documentUUID/mark-all-read` — `server/routes/readingProgress.ts:169` — Bulk mark read. [CONSENSUS: 1/4] (GLM)

### Search / Contextual Questions
- `GET /search` (universal) — `server/routes/search.ts` — Unified search across documents/blocks/highlights. [CONSENSUS: 2/4] (GLM + MiniMax SearchPage)
- `POST /contextual-questions` — `server/routes/contextualQuestions.ts:14` — Contextual questions for selected text. [CONSENSUS: 1/4] (GLM)

### Block Action Refinements (feedback→refinement)
- `POST /api/block-action-refinements/analyze` — `server/routes/blockActionRefinements.ts:65/103` — Analyze user feedback via `feedbackAnalysisService.ts:174/197` (LLM-proposed refinement with cost tracking). [CONSENSUS: 1/4] [OBS: missing withFeature] (Codex)
- `POST` approval path — `server/routes/blockActionRefinements.ts` + `blockActionRefinementService.ts:229` — TXN-insert refinement consumed at next prompt resolution via `promptIntegrationService.ts:684/692`. [CONSENSUS: 1/4] (Codex)

### MiniMax-only route surfaces
- `server/routes/arxiv.ts:1` + `arxivGrow.ts:1` — arXiv import + corpus-grow routes. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/blockRewrite.ts` (POST preview + POST `/:draftId/commit`) — Block rewrite preview/commit; Kimi contracts at `blockRewriteService.ts:187/312`. [CONSENSUS: 2/4] (MiniMax + Kimi)
- `server/routes/blogFunnel.ts` / `server/routes/blog/` — Blog authoring + funnel tracking. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/billing.ts` — Stripe webhook + plan. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/auth.ts` — Login + magic link + session. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/sandboxCallback.ts` + `chatGateway.ts` — Daytona sandbox callbacks + WebSocket chat gateway. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/bookExport.ts` — PDF/EPUB export. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/bookSearch.ts` — Corpus-level book search. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/bookmarks.ts` + `bookReadingProgress.ts` — Bookmarks + read-state. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/citationExport.ts` — BibTeX/RIS export. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/citations.ts` — Citation CRUD/dedup. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/claimBindings.ts` + `claims.ts` — Claim CRUD. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/contentNodes.ts` — Content node CRUD (consumes `pipelineOrchestratorService`). [CONSENSUS: 2/4] (MiniMax + Kimi)
- `server/routes/blockRevisions.ts` — Versioned block writes. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/blockExplainProfile.ts` — Per-block explain profile. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/adminOverview.ts` + `adminSystem.ts` + `adminAgentRuns.ts` + `adminBlogFunnel.ts` + `adminUnattendedRate.ts` + `adminVerificationHeatmap.ts` + `adminWhitelist.ts` — Backoffice JSON APIs. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/backgroundTasks.ts` — Long-task status. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/aiAnnotationWorker.ts` — Async annotation worker endpoint. [CONSENSUS: 1/4] (MiniMax)
- `server/routes/bookTreeGeneration.ts` — Generate markdown book tree structure. [CONSENSUS: 1/4] (MiniMax)

## React components / pages (135 features)

### Reader (markdown + PDF)
- `BookChapterArticle` — `src/pages/reader/BookChapterArticle.tsx:1` — Single book chapter render with markdown/highlights/inline annotations. [CONSENSUS: 2/4] (GLM + Kimi consumer)
- `BookReader` — `src/pages/reader/BookReader.tsx:1` — Top-level book reading container; route `/:lang/read/:bookUuid/:chapter?`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ReaderPage` — `src/pages/reader/ReaderPage.tsx:1` — Root reader router (markdown vs PDF branch). [CONSENSUS: 1/4] (GLM)
- `MarkdownChrome` — `src/components/libwit/reader/MarkdownChrome.tsx:1` — Chrome around markdown (scroll/highlights/selection toolbar). [CONSENSUS: 1/4] (GLM)
- `PdfChrome` — `src/components/libwit/reader/PdfChrome.tsx:1` — Chrome around PDF (margins/highlights/search). [CONSENSUS: 2/4] (GLM + MiniMax)
- `PdfSidebar` + `PdfMarginSidebar` — `src/components/libwit/reader/PdfSidebar.tsx:1` — PDF outline + margin annotations. [CONSENSUS: 1/4] (MiniMax)
- `ReaderShell` — `src/components/libwit/reader/shell/ReaderShell.tsx:1` — Full reader shell (top bar/spine/panels/ask bar). [CONSENSUS: 1/4] (GLM)
- `ReaderShellSpine` — `src/components/libwit/reader/shell/ReaderShellSpine.tsx:1` — Chapter spine TOC sidebar. [CONSENSUS: 1/4] (GLM)
- `ReaderShellAskBar` — `src/components/libwit/reader/shell/ReaderShellAskBar.tsx:1` — Inline ask bar (chat). [CONSENSUS: 1/4] (GLM)
- `ReaderShellSessions` — `src/components/libwit/reader/shell/ReaderShellSessions.tsx:1` — Session list/switcher panel. [CONSENSUS: 1/4] (GLM)
- `ReaderHighlightPalette` — `src/components/libwit/reader/ReaderHighlightPalette.tsx:1` — 5-color palette. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ReaderChatRail` — `src/components/libwit/reader/ReaderChatRail.tsx:1` — Side-rail chat. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ReaderFloatingChat` — `src/components/libwit/reader/ReaderFloatingChat.tsx:1` — Mobile floating chat overlay. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ReaderProgressStrip` — `src/components/libwit/reader/ReaderProgressStrip.tsx:1` — Footer progress %. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ReaderTranslationOverlay` — `src/components/libwit/reader/ReaderTranslationOverlay.tsx:1` — In-place translated render. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ReaderSettingsPopover` — `src/components/libwit/reader/ReaderSettingsPopover.tsx:1` — Typography/density/language. [CONSENSUS: 1/4] (MiniMax)
- `ChapterZoomControl` — `src/components/libwit/reader/ChapterZoomControl.tsx:1` — Zoom-into-chapter. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ChapterZoomTreeRail` + `ChapterZoomSummaryList` — `src/components/libwit/reader/ChapterZoomTreeRail.tsx:1` — 4-level zoom drilldown. [CONSENSUS: 1/4] (MiniMax)
- `BookToc` — `src/components/libwit/reader/BookToc.tsx:19` — Collapsible TOC with generation status ticks. [CONSENSUS: 2/4] (GLM + MiniMax)
- `CompareMode` — `src/components/libwit/reader/CompareMode.tsx:1` — Side-by-side chapter compare. [CONSENSUS: 2/4] (GLM + MiniMax)
- `PdfCompareMode` — `src/components/libwit/reader/PdfCompareMode.tsx:1` — PDF compare. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ShareProofButton` — `src/components/libwit/reader/ShareProofButton.tsx:1` — Public-link share. [CONSENSUS: 2/4] (GLM + MiniMax)
- `TimeToMasteryCounter` — `src/components/libwit/reader/TimeToMasteryCounter.tsx:1` — Mastery timer. [CONSENSUS: 2/4] (GLM + MiniMax)
- `VerifiedLearningCard` — `src/components/libwit/reader/VerifiedLearningCard.tsx:1` — Verified learning achievement card. [CONSENSUS: 2/4] (GLM + MiniMax)
- `SessionsPanel` — `src/components/libwit/reader/sessions/SessionsPanel.tsx:1` — Sessions browser. [CONSENSUS: 1/4] (GLM)
- `PdfSearchFloatPill` — `src/components/libwit/reader/search/PdfSearchFloatPill.tsx:1` — PDF search input. [CONSENSUS: 1/4] (GLM)
- Mobile reader shell — `src/components/libwit/reader/mobile/MobileReaderAskSelectionSheet.tsx` + `MobileReaderCiteSheet.tsx` + `MobileReaderFindSheet.tsx` + `MobileReaderHighlightsSheet.tsx` + `MobileReaderTOCSheet.tsx`. [CONSENSUS: 2/4] (GLM + MiniMax)

### Selection / highlighter toolbars
- `SelectionActionRow` — `src/components/selection/SelectionActionRow.tsx:104` — 7-action toolbar (visualize/explain/extend/arxiv/code/rewrite/prompt) with `lw-selection-action-{action}` testids. [CONSENSUS: 1/4] (MiniMax)
- `BlockSelectionToolbar` — `src/components/blocks/BlockSelectionToolbar.tsx:1` — Block-scoped count/delete/clear/ask-prompt. [CONSENSUS: 1/4] (MiniMax)
- `HighlighterToolbar` + `HighlighterHoverToolbar` + `ColorPalette` — `src/components/libwit/highlighter/` — Hover toolbar + 5-color palette. [CONSENSUS: 1/4] (MiniMax)
- `MobileSheet` — `src/components/libwit/highlighter/MobileSheet.tsx:1` — Mobile highlighter sheet. [CONSENSUS: 1/4] (MiniMax)

### Compose (book gen wizard)
- `ComposePage` — `src/pages/compose/ComposePage.tsx:1` — 11-step wizard. [CONSENSUS: 2/4] (GLM + MiniMax)
- `StepIntent` — `src/pages/compose/steps/StepIntent.tsx:1` [CONSENSUS: 1/4] (GLM)
- `StepTopic` — `src/pages/compose/steps/StepTopic.tsx:1` [CONSENSUS: 1/4] (GLM)
- `StepVoice` — `src/pages/compose/steps/StepVoice.tsx:1` [CONSENSUS: 1/4] (GLM)
- `StepStyleBlueprint` — `src/pages/compose/steps/StepStyleBlueprint.tsx:1` [CONSENSUS: 1/4] (GLM)
- `StepStyleSynthesis` — `src/pages/compose/steps/StepStyleSynthesis.tsx:1` — Publisher style synth. [CONSENSUS: 2/4] (GLM + MiniMax)
- `StepPublisherStyle` — `src/pages/compose/steps/StepPublisherStyle.tsx:1` [CONSENSUS: 1/4] (GLM)
- `StepPlanSketch` — `src/pages/compose/steps/StepPlanSketch.tsx:1` [CONSENSUS: 1/4] (GLM)
- `StepPlan` — `src/pages/compose/steps/StepPlan.tsx:1` [CONSENSUS: 1/4] (GLM)
- `StepRefineCitations` — `src/pages/compose/steps/StepRefineCitations.tsx:1` [CONSENSUS: 1/4] (GLM)
- `StepWrite` — `src/pages/compose/steps/StepWrite.tsx:1` — Live chapter-gen cockpit. [CONSENSUS: 1/4] (GLM)
- `StepConfigure` — `src/pages/compose/steps/StepConfigure.tsx:1` [CONSENSUS: 1/4] (GLM)
- `StepChapterStyleTuning` — `src/pages/compose/steps/StepChapterStyleTuning.tsx:1` [CONSENSUS: 1/4] (GLM)
- `useComposeMachine` + `jobStateMachine` + `skipLogic` — `src/pages/compose/useComposeMachine.ts:1` — Compose state machine + skip-logic. [CONSENSUS: 1/4] (MiniMax)
- `ProgressStrip` — `src/pages/compose/ProgressStrip.tsx:1` — Live chapter status strip. [CONSENSUS: 1/4] (MiniMax)
- `ComposePageMobile` — Mobile compose variant. [CONSENSUS: 1/4] (MiniMax)

### Blueprint
- `BlueprintReaderPreview` — `src/components/compose/blueprint/BlueprintReaderPreview.tsx:1` [CONSENSUS: 1/4] (GLM)
- `BlueprintRubricPanel` — `src/components/compose/blueprint/BlueprintRubricPanel.tsx:1` [CONSENSUS: 1/4] (GLM)
- `BlueprintSectionFeedbackPopover` — `src/components/compose/blueprint/BlueprintSectionFeedbackPopover.tsx:1` [CONSENSUS: 1/4] (GLM)
- `BlueprintSectionStrip` — `src/components/compose/blueprint/BlueprintSectionStrip.tsx:1` [CONSENSUS: 1/4] (GLM)

### Onboarding (dashboard wizard)
- `DashboardWizardSurface` — `src/components/libwit/onboarding/DashboardWizardSurface.tsx:1` — 8-step wizard host; route `/:lang/today?wizard=open`; anon-allowed. [CONSENSUS: 2/4] (GLM + MiniMax)
- `OnboardingSurvey` + `OnboardingSurveyLegacy` — `src/components/libwit/onboarding/OnboardingSurvey.tsx:1` — First-touch interview. [CONSENSUS: 2/4] (GLM + MiniMax)
- `SampleChapterVariantCard` — `src/components/libwit/onboarding/SampleChapterVariantCard.tsx:1` [CONSENSUS: 2/4] (GLM + MiniMax)
- `VariantGenerationStep` — `src/components/libwit/onboarding/VariantGenerationStep.tsx:1` [CONSENSUS: 2/4] (GLM + MiniMax)
- `VariantReviewStep` — `src/components/libwit/onboarding/VariantReviewStep.tsx:1` — Review/rate variants; submits section feedback (`SampleChapterWizard.tsx:112` → POST `/section-feedback`). [CONSENSUS: 3/4] (GLM + MiniMax + Codex flow)
- `SaveAndLandingStep` — `src/components/libwit/onboarding/SaveAndLandingStep.tsx:1` [CONSENSUS: 2/4] (GLM + MiniMax)
- `TopicPickerStep` — `src/components/libwit/onboarding/TopicPickerStep.tsx:1` [CONSENSUS: 2/4] (GLM + MiniMax)
- `SteerSectionPopover` — `src/components/libwit/onboarding/SteerSectionPopover.tsx:1` [CONSENSUS: 2/4] (GLM + MiniMax)
- `EvidenceMeter` — `src/components/libwit/onboarding/EvidenceMeter.tsx:1` [CONSENSUS: 2/4] (GLM + MiniMax)
- `InlineHint` — `src/components/libwit/onboarding/InlineHint.tsx:1` [CONSENSUS: 2/4] (GLM + MiniMax)
- `IntentModeChip` — `src/components/libwit/onboarding/IntentModeChip.tsx:1` [CONSENSUS: 1/4] (GLM)
- `RegisterModal` — `src/components/libwit/onboarding/RegisterModal.tsx:1` [CONSENSUS: 1/4] (GLM)
- `WizardStepper` — `src/components/libwit/onboarding/WizardStepper.tsx:1` [CONSENSUS: 1/4] (GLM)
- `WizardErrorModal` — `src/components/libwit/onboarding/WizardErrorModal.tsx:1` [CONSENSUS: 1/4] (GLM)
- `WizardLoader` — `src/components/libwit/onboarding/WizardLoader.tsx:1` [CONSENSUS: 1/4] (GLM)
- `wizardMachine` + `evidenceGate` — `src/components/libwit/onboarding/wizardMachine.ts:34` — State machine gating step 4 on planning convergence; `canAdvance` enforces `FEATURE_FLAGS.ONBOARDING_GATE_AT`. [CONSENSUS: 1/4] (MiniMax)
- Step bodies (StepIntent/StepClarify/StepProfile/StepPlan/StepLanding/StepRefreshConfirm/StepSample/StepSteer/StepFullBook) — `src/components/libwit/onboarding/steps/`. [CONSENSUS: 2/4] (GLM + MiniMax)

### Dashboard (today)
- `TodayPage` — `src/pages/TodayPage.tsx:1` + `TodayPageMobile.tsx` — Default landing. [CONSENSUS: 1/4] (MiniMax)
- `DashboardNavRail` + `DashboardNavRailCompact` + `navConfig` — `src/components/libwit/dashboard/DashboardNavRail.tsx:1` — Left rail. [CONSENSUS: 1/4] (MiniMax)
- `NowReadingCard` + `GuestSectionPreview` — `src/components/libwit/dashboard/NowReadingCard.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `ProjectPicker` + `ProjectColorSwatch` — `src/components/libwit/ProjectPicker.tsx:1` [CONSENSUS: 1/4] (MiniMax)

### Library, projects, search, graph
- `LibraryPage` + mobile — `src/pages/LibraryPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `ProjectsPage` + mobile — `src/pages/ProjectsPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `ProjectDetailPage` — `src/pages/ProjectDetailPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `SearchPage` + mobile — `src/pages/SearchPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `GraphPage` — `src/pages/GraphPage.tsx:1` — KG visualization across library. [CONSENSUS: 1/4] (MiniMax)

### Citations & bibliography
- `CitationsPage` + mobile — `src/pages/CitationsPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `CitationFilterPopover` + `CitationRow` + `BibliographyCard` + `TopConceptsCard` — `src/components/libwit/citations/` [CONSENSUS: 1/4] (MiniMax)
- `SemanticCiteBadge` — `src/components/reader/SemanticCiteBadge.tsx:1` [CONSENSUS: 1/4] (MiniMax)

### Chat
- `ChatPage` + mobile + `PageChat` — `src/pages/ChatPage.tsx:1` — Scope chips/sources rail/followups/composer; thumbs feedback at `ChatPage.tsx:509` → POST `/api/prompts/feedback`. [CONSENSUS: 2/4] (MiniMax + Codex)
- `Composer` + `FollowupChips` + `ScopeChips` + `SourcesRail` + `CitationCard` + `MessageBubble` + `AssistantMarkdownContent` — `src/components/libwit/chat/` [CONSENSUS: 1/4] (MiniMax)

### Memory & notebook
- `MemoryDashboard` + `MemoryDrawer` + `MemoryDialog` + `MemoryProvider` + `MemoryWidget` — `src/components/memory/MemoryDashboard.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `MemoryEditDialog` + `ForgetConfirmStrip` + `ForgetUndoToast` + `MemoryUndoToast` + `MemoryFreshBanner` [CONSENSUS: 1/4] (MiniMax)
- `MemoryFilterBar` + `MemoryEmptyState` + `MemoryLoadingSkeleton` [CONSENSUS: 1/4] (MiniMax)
- `RoutingChip` + `PagesReadSection` [CONSENSUS: 1/4] (MiniMax)
- `BranchInheritancePopover` + `AgentRunCard` + `AgentRunResumeConfirmDialog` [CONSENSUS: 1/4] (MiniMax)

### Quiz / exam
- `TodayReviewWidget` + `QuizModal` + `ExportQuizButton` + `QuizQuestionCard` + `QuizSetReview` + `ReviewCard` + `ReviewSession` — `src/components/learningQuiz/` [CONSENSUS: 1/4] (MiniMax)
- `ExamQuestionCard` + `PageExamQuestions` — `src/components/exam/` [CONSENSUS: 1/4] (MiniMax)

### Audio / podcast
- `AudiobookPanel` — `src/components/audiobook/AudiobookPanel.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `PodcastPanel` — `src/components/podcast/PodcastPanel.tsx:1` [CONSENSUS: 1/4] (MiniMax)

### Arxiv import
- `ArxivDeepDiveModal` + `ArxivDeepDiveListPanel` — `src/components/arxivDeepDive/` [CONSENSUS: 1/4] (MiniMax)
- `ArxivTooltip` + `ArxivRefEnhancer` + `ArxivModal` — `src/components/reader/` [CONSENSUS: 1/4] (MiniMax)

### Blocks & content authoring
- `BlockTreeRenderer` + `BlockRenderer` + `BlockEditor` — `src/components/blocks/BlockTreeRenderer.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `BlockContextMenu` + `BlockMemoryChip` [CONSENSUS: 1/4] (MiniMax)
- `FloatingPipelineProgressHost` + `FloatingRewritePreviewHost` + `FloatingRewritePreviewPanel` + `FloatingAnnotationHost` + `FloatingAnnotationPanel` + `FloatingPanelSnapOverlay` — Rewrite preview at `FloatingRewritePreviewPanel.tsx:421` renders thumbs feedback; refinement input at `:337`. [CONSENSUS: 2/4] (MiniMax + Codex)
- `CrossBlockPipelineDialogs` + `ContentActionAnchorRow` [CONSENSUS: 1/4] (MiniMax)
- `AnnotationAccordions` + `AnnotationBadge` + `AnnotationContentDialog` + `AnnotationPanelContent` + `BlockAnnotationBadgeOverlay` [CONSENSUS: 1/4] (MiniMax)
- `ExplanationFeedbackButtons` + `ExplanationFeedbackDialog` + `LLMOutputFeedback` — Posts to `/api/prompts/feedback` with sources `explanation` / generic. [CONSENSUS: 2/4] (MiniMax + Codex)
- `BlockInspectorOverlay` + `BlockInspectorDeleteDialog` + `BlockSubtreeView` + `BlockSelectionRangeRail` + `BlockSelectionMobileSheet` [CONSENSUS: 1/4] (MiniMax)
- `BlockSkeleton` + `AskBlockBadge` + `AskBlockBadgeOverlay` [CONSENSUS: 1/4] (MiniMax)
- `FeedbackThumbs` (atom) — `src/components/libwit/atoms/FeedbackThumbs.tsx:1` + `src/components/libwit/feedback/FeedbackThumbs.tsx:45` (shared button invoking `onFeedback`). [CONSENSUS: 2/4] (MiniMax + Codex)

### Approval, notifications
- `StrongApprovalDialog` + `ApprovalReasoningDetails` — `src/components/approval/StrongApprovalDialog.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `ResearchNotificationPanel` — `src/components/notifications/ResearchNotificationPanel.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `WorkspaceInvitationAcceptPage` — `src/pages/WorkspaceInvitationAcceptPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)

### Auth & settings
- `LoginPage` — `src/pages/LoginPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `MagicLinkConsumePage` — `src/pages/MagicLinkConsumePage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `AuthGate` — `src/components/libwit/auth/AuthGate.tsx:1/8` — Wraps `/:lang/*`; `anonAllowedMatcher` carve-out for dashboard wizard. [CONSENSUS: 1/4] (MiniMax)
- `ThemeSettingsPage` — `src/pages/admin/ThemeSettingsPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `LwSettings/` — Settings dir. [CONSENSUS: 1/4] (MiniMax)

### Admin / backoffice pages
- `AdminOverviewPage` — `src/pages/admin/AdminOverviewPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `AdminSystemPage` — `src/pages/admin/AdminSystemPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `AgentRunsPage` — `src/pages/admin/AgentRunsPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `AgentTrajectoryPage` — `src/pages/admin/AgentTrajectoryPage.tsx:1` + `agentTrajectory/` [CONSENSUS: 1/4] (MiniMax)
- `BookEditorToolsPage` — `src/pages/admin/BookEditorToolsPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `CompliancePage` — `src/pages/admin/CompliancePage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `ExecutionLedgerPage` — `src/pages/admin/ExecutionLedgerPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `FailureAnalyticsPage` — `src/pages/admin/FailureAnalyticsPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `GepaDashboardPage` — `src/pages/admin/GepaDashboardPage.tsx:1` + `gepa/` sub-components [CONSENSUS: 1/4] (MiniMax)
- `MemoryCalibrationPage` — `src/pages/admin/MemoryCalibrationPage.tsx:1` + `memoryCalibration/` [CONSENSUS: 1/4] (MiniMax)
- `PromptEvolutionPage` — `src/pages/admin/PromptEvolutionPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `PromptsAdminPage` — `src/pages/admin/PromptsAdminPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `SkillsAnalyticsPage` — `src/pages/admin/SkillsAnalyticsPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `StyleInspectorPage` — `src/pages/admin/StyleInspectorPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `SupportInboxPage` — `src/pages/admin/SupportInboxPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `UnattendedRatePage` — `src/pages/admin/UnattendedRatePage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `VerificationHeatmapPage` — `src/pages/admin/VerificationHeatmapPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `WhitelistAdminPage` — `src/pages/admin/WhitelistAdminPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)

### Landing & misc
- `LandingPage` — `src/pages/landing/` [CONSENSUS: 1/4] (MiniMax)
- `NotFoundPage` — `src/pages/NotFoundPage.tsx:1` [CONSENSUS: 1/4] (MiniMax)
- `PerformanceMonitoringDashboard` — `src/components/FromTheDocs.tsx:1` [CONSENSUS: 1/4] (MiniMax)

## Background jobs / workers / cron (75 features)

### Book Generation Pipeline
- `BookSetupProcessor` — `server/workers/unified/processors/bookSetupProcessor.ts:48/105` — Initializes job + overview/chapter planning context. Queue `book_generation_setup`. [CONSENSUS: 3/4] [STATUS: shipped] (GLM + Codex flow + MiniMax)
- `PlanGenerationProcessor` — `server/workers/unified/processors/planGenerationProcessor.ts` — Plan generation with evidence cascade. Queue `plan_generation`. [CONSENSUS: 1/4] (GLM)
- `ChapterGenerationProcessor` — `server/workers/unified/processors/chapterGenerationProcessor.ts:45/138/144` — Single-chapter generation via sandbox; retry-aware completion. Queue `chapter_generation`. [CONSENSUS: 3/4] [STATUS: shipped, no resume-from-checkpoint per MiniMax gap #1] (GLM + Codex + MiniMax)
- `ChapterPlanFillProcessor` — `server/workers/unified/processors/chapterPlanFillProcessor.ts` — Backfills chapter plan details before writing. Queue `chapter_plan_fill`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `BookFinalizeProcessor` — `server/workers/unified/processors/bookFinalizeProcessor.ts` — Finalize close-out (appendixes/cross-refs/rubric). Queue `book_finalize`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `BookCoverGenerationProcessor` — `server/workers/unified/processors/bookCoverGenerationProcessor.ts` + `bookCoverService.ts` — Imagen cover. Queue `book_cover_generation`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `BookGenerationProcessor` (parent) — `server/workers/unified/processors/bookGenerationProcessor.ts:1` — Parent runner for chapter-fanout. [CONSENSUS: 1/4] (MiniMax)

### Post-Gen & Assurance
- `ChapterAbstractProcessor` — `server/workers/unified/processors/chapterAbstractProcessor.ts` — Per-chapter TL;DR. Queue `chapter_abstract`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ChapterIllustrationProcessor` — `server/workers/unified/processors/chapterIllustrationProcessor.ts` — Imagen brief→image→attachment. Queue `chapter_illustration`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ChapterZoomRegenerateProcessor` — `server/workers/unified/processors/chapterZoomRegenerateProcessor.ts` — Re-render zoom level. Queue `chapter_zoom_regen`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ChapterHierarchyIndexingProcessor` — `server/workers/unified/processors/chapterHierarchyIndexingProcessor.ts` — Hierarchy index. Queue `chapter_hierarchy_index`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ClaimRefinementRegenProcessor` — `server/workers/unified/processors/claimRefinementRegenProcessor.ts` — Regenerate refined claims. Cron `queue:claim-refinement-regen`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `BookTreeGeneration` processor — generates markdown book tree. [CONSENSUS: 1/4] (MiniMax)
- `PageSummarizationProcessor` — `pageSummarizationProcessor.ts:1` — Per-page TL;DR. [CONSENSUS: 1/4] (MiniMax)
- `OrphanConnectionProcessor` — `orphanConnectionProcessor.ts:1` — Link orphan blocks to nearest parent. [CONSENSUS: 1/4] (MiniMax)
- `ExamQuestionsProcessor` — `examQuestionsProcessor.ts:1` — Per-chapter exam questions. [CONSENSUS: 1/4] (MiniMax)
- `ChromeLockedProcessor` — `chromeLockedProcessor.ts:1` — Chrome-locked chapter reflow. [CONSENSUS: 1/4] (MiniMax)

### Translation & Podcast
- `BookTranslationProcessor` — `server/workers/unified/processors/bookTranslationProcessor.ts` — Book chapters translate with `<<<PRESERVE_n>>>`. Queue `book_translation`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `TranslationProcessor` — `server/workers/unified/processors/translationProcessor.ts` — General document translation. Queue `translation`. [CONSENSUS: 1/4] (GLM)
- `PodcastGenerationProcessor` — `server/workers/unified/processors/podcastGenerationProcessor.ts` — Podcast audio. Queue `podcast_generation`. [CONSENSUS: 1/4] (GLM)
- `TtsPreparationProcessor` — `server/workers/unified/processors/ttsPreparationProcessor.ts` — TTS prep. Queue `tts_prep`. [CONSENSUS: 1/4] (GLM)

### Memory & Context
- `MemoryCollectionProcessor` — `server/workers/unified/processors/memoryCollectionProcessor.ts` — Collect/store memories. Cron `cron:memory_drift`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `MemoryDriftCron` — `server/workers/unified/processors/memoryDriftCron.ts` — Memory drift detection. Cron `cron:memory_drift`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ForgottenMemoryPrune` — `server/workers/unified/processors/forgottenMemoryPrune.ts` — TTL prune. Cron `cron:forgotten-memory-prune`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `MemoryExtractionProcessor` — `server/workers/memoryExtractionProcessor.ts` — Extract memories from activity. Queue `memory_extraction`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ContextDistill` — `server/workers/unified/processors/contextDistill.ts` — Distill context. Cron `cron:context_distill`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ContextFsrsDecay` — `server/workers/unified/processors/contextFsrsDecay.ts` — FSRS decay. Cron `cron:context_fsrs_decay`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ContextReconcile` — `server/workers/unified/processors/contextReconcile.ts` — Reconcile Qdrant↔Postgres drift. Cron `cron:context_reconcile`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ContextCacheReap` — `server/workers/unified/processors/contextCacheReap.ts` — Reap stale context cache. Cron `cron:context_cache_reap`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ContextEngineEval` — `server/workers/unified/processors/contextEngineEval.ts` — Retrieval precision/recall eval. Cron `cron:context-engine-eval`. [CONSENSUS: 2/4] (GLM + MiniMax)

### Prompt Evolution (GEPA) Crons
- `PromptEvolutionCronDispatch` — `server/workers/unified/processors/promptEvolutionCronDispatch.ts:64/101/111` — Dispatches GEPA + shadow-test routing. Declares cron feature `cron:prompt-evolution-dispatch`. [CONSENSUS: 3/4] [STATUS: shipped] (GLM + Codex flows + MiniMax)
- `GepaCron` quarterly — `server/services/promptEvolution/gepaCron.ts:61/72/247/315/464` + `server/app.ts:874` — Quarterly mutation cycle (cron `0 2 1-7 1,4,7,10 *`); loads samples from `explanation_feedback`, composes mutations bypassing `promptIntegrationService` (`:315`). [CONSENSUS: 2/4] (GLM + Codex)
- `ConcludeShadowCron` hourly — `server/services/promptEvolution/concludeShadowCron.ts:37/56/79` + `concludeShadowTest.ts:106/129/180/215` + `server/app.ts:884` — Cron `7 * * * *`; advisory lock + pending-run selection + promotion. [CONSENSUS: 2/4] (GLM + Codex)
- `LlmJudgeCron` nightly — `server/cron/index.ts:35/52` + `server/app.ts:904` — Cron `0 2 * * *` upserts `llm_judge_nightly` but Codex flagged DEAD PATH at `server/workers/unified/workers/index.ts:1208/1210` — scheduled-pipelines worker doesn't route this pipeline type. [CONSENSUS: 2/4] [STATUS: broken/dead-path per Codex] (GLM + Codex)
- `RedTeamProcessor` — `server/workers/unified/processors/redTeamProcessor.ts` — Adversarial drill. Cron `cron:red-team`. [CONSENSUS: 1/4] (GLM)
- `QualityDriftProcessor` — `server/workers/unified/processors/qualityDriftProcessor.ts` — Quality drift scan. Cron `cron:quality-drift-scan`. [CONSENSUS: 1/4] (GLM)
- `PersonalityDriftProcessor` — `server/workers/unified/processors/personalityDriftProcessor.ts` — Personality vectors. Cron `cron:personality-drift-detect`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `PreferenceLearningProcessor` — `server/workers/unified/processors/preferenceLearningProcessor.ts` — Consolidate prefs. Cron `cron:preference-learning-consolidation`. [CONSENSUS: 1/4] (GLM)

### BCE (Book Co-Evolution) Crons
- `BceCronDispatch` — `server/workers/unified/processors/bceCronDispatch.ts:78/92/120/140/164/185/206` — Declares feature `cron:bce-dispatch`; routes pipeline types to trajectory/profile/skill/memory handlers. Skips unrecognized types `:92`. [CONSENSUS: 2/4] (GLM + Codex)
- BCE cron registry — `server/services/bookGeneration/coEvolution/cronRegistry.ts:39/61/74/87/100` — Flag-gated registration of trajectory-extraction / profile-update / skill-distillation / memory-decay. [CONSENSUS: 1/4] (Codex)

### Knowledge & Evidence
- `KnowledgeExtractionProcessor` (via `aiAnnotationProcessor.ts:1`) — AI-powered block annotation. Queue `ai_annotation`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ArtifactGenerationProcessor` — `server/workers/unified/processors/artifactGenerationProcessor.ts` — Content artifact generation. Queue `artifact_generation`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `SuggestionNodesProcessor` — `server/workers/unified/processors/suggestionNodesProcessor.ts` — KG suggestion nodes. Queue `suggestion_nodes`. [CONSENSUS: 1/4] (GLM)
- `DocumentIndexingProcessor` — `documentIndexingProcessor.ts:1` + `backgroundIndexingService.ts:1` — Qdrant upsert. [CONSENSUS: 1/4] (MiniMax)
- `HierarchyBuildProcessor` — `hierarchyBuildProcessor.ts:1` + `hierarchyBuildService.ts` — Rebuild hierarchy index. [CONSENSUS: 1/4] (MiniMax)
- `CascadeInvalidationProcessor` — `cascadeInvalidationProcessor.ts:1` — Per-edit downstream cache invalidation. Cron `cron:cascade_invalidation`. [CONSENSUS: 2/4] (GLM + MiniMax)

### Infrastructure & Maintenance
- `ArxivHarvestProcessor` — `server/workers/unified/processors/arxivHarvestProcessor.ts` — Cron `kb:arxiv:harvest` every ~6h. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ArxivGrowService` — `server/services/arxivGrowService.ts:1` + `arxivGrow.ts` route — Corpus expand on threshold. [CONSENSUS: 1/4] (MiniMax)
- `AnonymousArxivFunnelService` — `anonymousArxivFunnelService.ts:1` — Anon→arxiv funnel tracking. [CONSENSUS: 1/4] (MiniMax)
- `StuckJobHealthChecker` — `server/workers/unified/processors/stuckJobHealthChecker.ts` — Cron `cron:stuck-job-health-check`. [CONSENSUS: 1/4] (GLM)
- `BackgroundTaskPruneProcessor` — `server/workers/unified/processors/backgroundTaskPruneProcessor.ts` — Cron `cron:background-task-prune`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `ImplicitAcceptanceProcessor` — `server/workers/unified/processors/implicitAcceptanceProcessor.ts` — Cron `cron:implicit-acceptance-check`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `SandboxResultReconciler` — `server/workers/unified/processors/sandboxResultReconciler.ts` — Cron `cron:sandbox-result-reconciler`. [CONSENSUS: 1/4] (GLM)
- `SkillPromotionProcessor` — `server/workers/unified/processors/skillPromotionProcessor.ts` — Promote learned skills. Cron `cron:skill_promotion`. [CONSENSUS: 1/4] (GLM)
- `BlockClassifierRepairProcessor` — `blockClassifierRepairProcessor.ts:1` — Reclassify low-confidence blocks. Cron `queue:classifier-repair`. [CONSENSUS: 2/4] (GLM + MiniMax)
- `BookGen stalled-job recovery` — `server/services/bookGeneration/lifecycle.ts:2612/2642/2661` + `server/app.ts:1140/1186` — In-process interval every 5min wrapped in `withFeature({name:'book-gen:orchestrator'})`. [CONSENSUS: 1/4] (Codex)

### Feedback (preference drift)
- `PreferenceDriftCron` monthly — `server/services/feedback/preferenceDriftCron.ts:19/30` + `server/app.ts:935` — Cron `0 5 1 * *` upserts `preference_drift_monthly`. DEAD PATH at `server/workers/unified/workers/index.ts:1208/1210` (scheduled-pipelines worker doesn't route this). `preferenceDriftService.ts:46/182` exists but unreachable. [CONSENSUS: 2/4] [STATUS: broken/dead-path per Codex] (GLM + Codex)

### Hatchet sample chapter task
- `sampleChapterTask.ts:125/162` — Resolves system+user prompts through `promptIntegrationService`; streams LLM output with cost tracking. [CONSENSUS: 1/4] (Codex)

## Database tables / migrations (13 features)

- `gepa_mutations` — `server/database/migrations/20260910000000_gepa_mutations_evolution_run_id.sql` — GEPA mutation records with evolution_run_id FK. [CONSENSUS: 1/4] (GLM)
- `evolution_runs` rollback support — `20260910000001_evolution_run_rollback_support.sql` — Evolution run tracking with rollback columns. [CONSENSUS: 1/4] (GLM)
- `gepa_mutations_by_vertical` — `20260910000002_gepa_mutations_by_vertical.sql` — Vertical-indexed mutations. [CONSENSUS: 1/4] (GLM)
- `gepa_mutations_hitl` — `20260910000003_gepa_mutations_hitl.sql` — HITL decision tracking. [CONSENSUS: 1/4] (GLM)
- `quiz_indexes` — `20260815000000_quiz_indexes.sql` — Quiz item index optimization. [CONSENSUS: 1/4] (GLM)
- `v4_bridge_block_derivations` — `20260820000000_v4_bridge_block_derivations_agent_state.sql` — Block derivation tracking with agent state. [CONSENSUS: 1/4] (GLM)
- `content_artifacts` — `20260821000000_v4_content_artifacts.sql` — Content artifact storage. [CONSENSUS: 1/4] (GLM)
- `content_action_anchors` — `20260824000000_content_action_anchors.sql` — Action anchor storage. [CONSENSUS: 1/4] (GLM)
- `deep_research_artifact_kind` — `20260825000000_v4_deep_research_content_artifact_kind.sql` — Deep research artifact type. [CONSENSUS: 1/4] (GLM)
- `verification_results_quiz_item_target` — `20260826000000_v4_verification_results_quiz_item_target.sql` — Quiz verification results. [CONSENSUS: 1/4] (GLM)
- `sample_chapter_artifact_kind` — `20260827000000_v4_sample_chapter_content_artifact_kind.sql` — Sample chapter artifact type. [CONSENSUS: 1/4] (GLM)
- `world_model_v2_simulation_result` — `20260903000001_world_model_v2_awm_2_simulation_result.sql` — World model simulation results. [CONSENSUS: 1/4] (GLM)
- `anchor_uuid_unique_idx` — `20260903000002_anchor_uuid_unique_idx.sql` — Unique anchor UUID index. [CONSENSUS: 1/4] (GLM)

### Tables referenced by services (without explicit migration cite)
- `blocks` — multi-row inserts with MD5 `content_hash` + `blocks_user_uuid_required_on_chapter_content` CHECK constraint (Kimi `blockService.ts:81`); `source_type='book_chapter_anchor'` + `node_type='chapter'` shape for chapter body anchor (per CLAUDE.md)
- `block_rewrite_drafts` — Stream Gemini output rows; `committed_at` close-out (Kimi `blockRewriteService.ts:187/312`)
- `book_generation_runs` / `book_generation_jobs` — Lifecycle row used for plan + sample variants (Codex `lifecycle.ts:594`, `variantDispatch.ts:159`)
- `prompt_executions` — Pending ledger row created on every prompt resolution (Codex `promptIntegrationService.ts:1055` + `promptExecutionService.ts:36`)
- `prompt_templates` — UPSERT'd at boot via `syncPromptRegistryToDatabase()` (Codex `server/app.ts:756`)
- `prompt_experiments`, `user_prompt_settings`, `document_prompt_settings`, `block_action_refinements` — Resolution chain reads (Codex `:537/584/610/634/684`)
- `style_blueprint_samples`, `blueprint_section_feedback`, `blueprint_rubrics` — Sample chapter harvest persistence (Codex `samplePersistence.ts:39/150` + `sessionStore.ts:145/168/187`)
- `explanation_feedback` — Unified target table for chat/explanation/rewrite feedback (Codex + Kimi)
- `documents` + `processed_content_cache` — SHA-256 dedup (Kimi `documentUploadPipeline.ts:94`)
- `background_tasks` — Task tracking via `treeTaskService` + `notifyWs` (Kimi `backgroundTaskService.ts:104`); `metadata.trace_dimensions` JSONB updated by `agentTraceLogger.flush` (Kimi `:76`)
- `llm_api_calls` — Cost recording from gateway + Claude SDK + Gemini (Kimi `geminiClient.ts:104` + `claudeAgentSdk.ts:87`)
- `publisher_styles` — Calibration bucket persistence (MiniMax + CLAUDE.md learnings)

## Prompt registry keys (46 features)

### Book Generation
- `prompt_book_plan_gen` — `server/services/bookGeneration/planGen.ts:46` — Full book plan generation. [CONSENSUS: 1/4] (GLM)
- `prompt_plan_judge` — `server/services/bookGeneration/planGen.ts:72` — Plan quality judge. [CONSENSUS: 1/4] (GLM)
- `prompt_plan_refinement` — `server/services/bookGeneration/planGen.ts:99` — Plan refinement iterations. [CONSENSUS: 1/4] (GLM)
- `prompt_writing_style_guide` — `server/services/bookGeneration/planGen.ts:132` — Writing style guide. [CONSENSUS: 1/4] (GLM)
- `prompt_chapter_planner` — `server/services/bookGeneration/chapterPlannerService.ts:150` — Chapter planning with evidence; Kimi `:104` contract uses Kimi Code Plan API + reads `publisher_styles` for calibration. [CONSENSUS: 2/4] (GLM + Kimi)
- `prompt_capsule_merged_draft` — `server/services/bookGeneration/chapterWriterCapsule.ts:119` — Merged draft for capsule writer. [CONSENSUS: 1/4] (GLM)
- `prompt_chapter_abstract_extraction` — `server/services/bookGeneration/postGen.ts:42` [CONSENSUS: 1/4] (GLM)
- `prompt_glossary_extraction` — `server/services/bookGeneration/postGen.ts:74` [CONSENSUS: 1/4] (GLM)
- `prompt_crossref_generation` — `server/services/bookGeneration/postGen.ts:99` [CONSENSUS: 1/4] (GLM)
- `prompt_book_introduction` — `server/services/bookGeneration/postGen.ts:125` [CONSENSUS: 1/4] (GLM)
- `prompt_chapter_comprehension_quiz` — `server/services/bookGeneration/quizAutoGen.ts:62` [CONSENSUS: 1/4] (GLM)
- `prompt_source_claim_extract` — `server/services/bookGeneration/evidenceBank/sourceClaimExtractor.ts:54` [CONSENSUS: 1/4] (GLM)
- `prompt_chapter_evidence_pack` — `server/services/bookGeneration/chapterEvidencePackAssembler.ts:53` [CONSENSUS: 1/4] (GLM)
- `prompt_skill_distiller` — `server/services/bookGeneration/memory/skillDistiller.ts:78` [CONSENSUS: 1/4] (GLM)
- `prompt_trajectory_extractor` — `server/services/bookGeneration/memory/trajectoryExtractor.ts:67` [CONSENSUS: 1/4] (GLM)
- `prompt_chapter_genre_escalation` — `server/services/bookGeneration/chapterWritingContract/escalateGenreLlm.ts:78/140/167/190/216/236` — Registered + resolved via `promptIntegrationService` + Gemini via `traceGemini`; returns null on failure (graceful degrade). [CONSENSUS: 1/4] [STATUS: shipped with traceGemini] (Codex)

### Co-Evolution
- `prompt_coevo_outline` — `server/services/bookGeneration/coEvolution/promptRegistry.ts:77` [CONSENSUS: 1/4] (GLM)
- `prompt_coevo_chunk` — `:100` [CONSENSUS: 1/4] (GLM)
- `prompt_coevo_assert` — `:114` [CONSENSUS: 1/4] (GLM)
- `prompt_coevo_verify` — `:132` [CONSENSUS: 1/4] (GLM)
- `prompt_coevo_repair` — `:154` [CONSENSUS: 1/4] (GLM)
- `prompt_vertical_classifier` — `server/services/bookGeneration/coEvolution/verticalClassifierLLM.ts:45/105/178/203/227/246` — Registered + resolved + structured LLM call with cost tracking; optional BAML override. [CONSENSUS: 2/4] [OBS: missing — no local withFeature/traceGemini/OTel span] (GLM + Codex)

### Structural Synthesis
- `prompt_evidence_cluster_summary` — `server/services/bookGeneration/structuralSynthesis/evidenceClusterSummary.ts:44/86/130/182/205/225` — Registered + iterates clusters + structured LLM with cost tracking; persists cluster summaries. [CONSENSUS: 2/4] [OBS: missing] (GLM + Codex)
- `prompt_oracle_outline` — `server/services/bookGeneration/structuralSynthesis/oracleOutlineExtractor.ts:56` [CONSENSUS: 1/4] (GLM)
- `prompt_multi_aspect_taxonomy` — `server/services/bookGeneration/structuralSynthesis/multiAspectTaxonomy.ts:71` [CONSENSUS: 1/4] (GLM)

### Source Ingestion
- `prompt_reference_extractor` — `server/services/bookGeneration/sourceIngestion/referenceExtractor.ts:41` [CONSENSUS: 1/4] (GLM)
- `prompt_user_synthesis` — `server/services/bookGeneration/sourceIngestion/userSynthesisExtractor.ts:49` [CONSENSUS: 1/4] (GLM)

### Publisher Style
- `prompt_synthesize_publisher_style` — `server/services/publisherStyle/synthesizeStyle.ts:90` [CONSENSUS: 1/4] (GLM)
- `prompt_create_publisher_style_spec` — `server/services/publisherStyle/synthesizePublisherStylePrompt.ts:42` [CONSENSUS: 1/4] (GLM)

### Assurance
- `prompt_coverage_audit` — `server/services/bookGeneration/assurance/coverageAuditService.ts:63` [CONSENSUS: 1/4] (GLM)
- `prompt_reader_quiz` (5 variants) — `server/services/bookGeneration/assurance/readerQuizGate.ts:204-256` [CONSENSUS: 1/4] (GLM)

### Prompt Evolution (GEPA)
- `prompt_adversarial_generator` — `server/services/promptEvolution/adversarialDrill.ts:64` [CONSENSUS: 1/4] (GLM)

### Onboarding
- `prompt_onboarding_sample_chapter_system` — `server/routes/onboardingSampleChapter.ts:58` [CONSENSUS: 1/4] (GLM)
- `prompt_onboarding_sample_chapter_user` — `:69` [CONSENSUS: 1/4] (GLM)
- `prompt_onboarding_intent_classifier` — `:96` [CONSENSUS: 1/4] (GLM)
- `prompt_onboarding_clarifying_questions` — `:112` [CONSENSUS: 1/4] (GLM)
- `prompt_onboarding_plan_draft` — `:138` [CONSENSUS: 1/4] (GLM)
- `prompt_contextual_questions` — `server/routes/contextualQuestions.ts:15` [CONSENSUS: 1/4] (GLM)

### Feedback / Refinement
- `prompt_feedback_analysis` — `server/services/feedbackAnalysisService.ts:174/197` — Feedback-to-refinement analyzer resolved through the harness; LLM call with cost tracking. [CONSENSUS: 1/4] (Codex)
- `prompt_block_rewrite_text` + `prompt_block_rewrite_image_brief` — `server/services/blockRewriteService.ts` — In-process Gemini rewrites (per CLAUDE.md reference implementations). [CONSENSUS: 2/4] (CLAUDE.md + Kimi `:187/312`)
- `prompt_expand_paragraph` — `server/services/pipelineTemplates/expandParagraph.ts` — Pipeline-resolved at build (per CLAUDE.md). [CONSENSUS: 1/4] (CLAUDE.md ref)
- `prompt_rag_chat_response` + `prompt_rag_chat_system` — `server/services/chatSessionService.ts` — Chat surface prompts (per CLAUDE.md). [CONSENSUS: 2/4] (CLAUDE.md + Kimi `:312`)
- `MARKDOWN_RENDERING_CONTRACT` placeholder — `server/services/promptFragments/outputDirective.ts` — Mandatory `outputDirective` injection for all markdown-producing prompts. [CONSENSUS: 1/4] (CLAUDE.md reference)

## CLI scripts (0 features)

- None surfaced by lenses (no lens covered `scripts/`, `tools/`, or `.agentflow/` shell entrypoints in this run).

## Service contracts (Kimi single-lens depth — 25 features)

These are not separate features from the routes/workers above; they are Kimi's per-service input/output/error-mode contracts that document side-effects + ambiguity markers.

- `blockService.createBlocksBatchWithClient` — `server/services/blockService.ts:81` — Multi-row INSERT; MD5 content_hash; CHECK constraint on chapter content user_uuid.
- `blockRewriteService.previewRewrite` — `server/services/blockRewriteService.ts:187` — INSERT draft + Gemini stream + `llmCostService` + idempotency. [CONTRACT: ambiguous — fallback HTML silently degrades]
- `blockRewriteService.commitRewrite` — `:312` — TXN: subtree DELETE + new INSERT; emits `block_rewrite_committed`. [CONTRACT: ambiguous — no workspace-scoped ownership check beyond userUuid]
- `bookOrchestrator.runGeneration` — `server/services/bookOrchestrator.ts:104` — Reads/writes `book_generation_jobs` status; spawns `ChapterGenerator` per level. [CONTRACT: ambiguous — partial success: `success=true` even if some chapters failed]
- `chapterPlannerService.planChapter` — `chapterPlannerService.ts:104` — Calls Kimi Code Plan API; Zero-Fallback throws on schema parse failure or invariant violation.
- `BaseSandboxAgent.execute` — `server/services/daytona/baseSandboxAgent.ts:178` — Creates Daytona sandbox; upload/run/download via `sandbox.fs.uploadFile`/`downloadFile`; preserves on failure. [CONTRACT: ambiguous — abstract `getTimeoutMs()` ranges 5min–30min across subclasses]
- `documentUploadPipeline.validateAndStore` — `documentUploadPipeline.ts:94` — SHA-256 dedup + arXiv URL→PDF rewrite + modes `fresh|resume|copy_from_cache`. [CONTRACT: ambiguous — resume mode reuses row without workspaceUuid match]
- `chatSessionService.sendFirstMessage` — `chatSessionService.ts:312` — Streams LLM via `llmStream`; upserts sessions/messages; SURFACE_DEPTH cap (default 3). [CONTRACT: ambiguous — surfaceDepth hard-throw but RAG failure silent-swallow]
- `promptIntegrationService.resolvePromptForDocument` — `promptIntegrationService.ts:104` — Resolution chain: experiment→document→user→production→refinement→placeholder injection. [CONTRACT: ambiguous — silent fallback to base template masks data corruption; no metric emitted]
- `geminiClient.generateContent` — `geminiClient.ts:104` — Delegates to Vercel AI SDK gateway since 2026-05-14; legacy `gemini-*` IDs map to DeepSeek tiers; cost via `traceGemini`. (Image/file methods still hit Gemini directly.)
- `pipelineExecutor.executeNode` — `pipelineExecutor.ts:187` — DAG node execution; throws on cyclic dep / unresolvable `{{nodeId.outputKey}}`. [CONTRACT: ambiguous — fail-open per node; pipeline continues unless edge condition gates]
- `pipelineOrchestratorService.runPipeline` — `pipelineOrchestratorService.ts:187` — Resolves prompts + builds context + dispatches; fail-closed at pipeline if ANY node fails.
- `contentNodeService.generateChildContent` — `contentNodeService.ts:312` — Calls Gemini (explain/extend) or Claude Agent SDK (viz); enqueues `QUEUE_NAMES.SUGGESTION_NODES`. [CONTRACT: ambiguous — unparseable LLM output logs warning but still inserts empty-content blocks]
- `entityBlockService.createNoteBlock` — `entityBlockService.ts:104` — Decomposes markdown via `parseMarkdownLines→buildHierarchy→flattenBlocksWithUuid`; batch-insert. [CONTRACT: ambiguous — non-transactional by default; caller must pass PoolClient]
- `backgroundTaskService.createTask` — `backgroundTaskService.ts:104` — INSERT + lazy `notifyWs`. [CONTRACT: ambiguous — WS failure silent; FE may never see task]
- `claudeAgentSdk.query` — `claudeAgentSdk.ts:87` — SDK call with OTel + cost recording; warns under `feature_name='claude_unknown'` if tracking omitted.
- `embeddingService.generateEmbeddingsBatch` — `embeddingService.ts:104` — Routes to `local` (768-dim Python) or `gemini` (3072-dim) via `EMBEDDING_PROVIDER`. [CONTRACT: ambiguous — empty input returns empty array instead of throw]
- `agentTraceLogger.flush` — `agentTraceLogger.ts:76` — UPDATE `background_tasks.metadata.trace_dimensions` JSONB + Loki via `createFeatureLogger`. [CONTRACT: ambiguous — trace loss on DB failure; no retry/queue]
- `bookMermaidValidator.validateMermaidNodeIds` — `bookMermaidValidator.ts:187` — Pure fn; detects 9 issue classes (html_tags / unescaped_parentheses / pipe_in_label / invalid_syntax / malformed_node / newline_in_note / hardcoded_color_in_caption / embedded_markdown / standalone_semantic_class). [CONTRACT: ambiguous — regex may miss nested code blocks]
- `preservationTokens.wrapPreservationTokens` — `server/services/bookTranslation/preservationTokens.ts:48` — Pure fn; wraps code/math/citation/arXiv/author-voice in `<<<PRESERVE_n>>>`. [CONTRACT: ambiguous — nested code↔math may produce incorrect tokenization]
- `useCrossBlockHighlight` — `src/hooks/useCrossBlockHighlight.ts:104` — Attaches `mouseup` listener; calls `textHighlightService.createHighlight` + `blockApiClient`/`contentNodeApiClient`. [CONTRACT: ambiguous — race condition: rapid mouseup may fire overlapping createHighlight before pendingBatchRef gate]
- `useTextHighlights` — `src/hooks/useTextHighlights.ts:104` — Fetches via `textHighlightCache` + subscribes to updates. [CONTRACT: ambiguous — cache subscription leaks if unmount during fetch]
- `blockMigrationService.migrateBlocksToChapter` — `blockMigrationService.ts:104` — Copies tree with new UUIDs; preserves `order_idx`/`parent_uuid`. [CONTRACT: ambiguous — no UPSERT logic; idempotent only if caller guarantees clean target]

## Cross-cutting flows (Codex single-lens — 21 chains)

End-to-end chains with file:line evidence per step (deduplicated where the chain hits surfaces above):

- Prompt registry boot → DB sync — `server/app.ts:704/756` → `promptIntegrationService.ts:508/1055` → `promptExecutionService.ts:36`. [OBS: missing for boot sync itself]
- Generic prompt resolution + enrichment — `promptIntegrationService.ts:537/584/610/634/684/704/1044/1091`. [OBS: missing — fallback returns no execution id]
- Chat thumbs feedback (FE+BE full-stack) — `FeedbackThumbs.tsx:45` → `ChatPage.tsx:509` → `chatSessionService.ts:382` → `promptFeedback.ts:32/55` → `unifiedPromptFeedbackService.ts:94/125/232`. [STATUS: shipped]
- Explanation + LLM-output feedback — `ExplanationFeedbackButtons.tsx:53` + `LLMOutputFeedback.tsx:114` → `promptFeedback.ts:32` → `unifiedPromptFeedbackService.ts:196/216` → `explanationFeedbackService.ts:47/154` (Langfuse score).
- Block rewrite preview feedback — `FloatingRewritePreviewPanel.tsx:421` → `rewritePreviewStore.ts:95` → `blockRewrite.ts:634` → `promptFeedback.ts:32` → `unifiedPromptFeedbackService.ts:148/167/174/185`.
- Feedback-to-refinement (analyze+approve) — `FloatingRewritePreviewPanel.tsx:337` → `blockActionRefinementApiClient.ts:61/80` → `blockActionRefinements.ts:65/103` → `feedbackAnalysisService.ts:174/197` → `blockActionRefinementService.ts:229`. [OBS: missing]
- Active refinement consumption — `promptIntegrationService.ts:684/692` → `blockActionRefinementService.ts:175` → `:1044/1055`.
- Sample chapter variant generation — `sampleChapterHarvest.ts:223/237` → `variantDispatch.ts:110/159` → `sampleVariantEnqueue.ts:42/74` → `sampleChapterTask.ts:125/162` → `samplePersistence.ts:39`. [OBS: missing route withFeature]
- Sample chapter section feedback — `VariantReviewStep.tsx:99` → `SampleChapterWizard.tsx:112` → `sampleChapterHarvest.ts:386/414` → `samplePersistence.ts:134/150`. [OBS: missing]
- Sample chapter session fetch — `sampleChapterHarvest.ts:447` → `sessionStore.ts:137/145/168/187/206`. [STATUS: shipped, OTel span present]
- Book draft creation — `bookGeneration.ts:389/401` → `lifecycle.ts:670/690`. [OBS: missing]
- Plan-only generation — `bookGeneration.ts:887/900/924` → `lifecycle.ts:659` (`withFeature`) → `bookGenerationQueueService.ts:371/417`.
- Confirmed generation + chapter writing — `bookGeneration.ts:955/967/988` → `bookGenerationQueueService.ts:207/331` → `bookSetupProcessor.ts:48/105` → `chapterGenerationProcessor.ts:45/138/144`.
- Job start with context distillation — `bookGeneration.ts:1029/1039/1054/1086` → `lifecycle.ts:552/594`.
- Plan + chapter steering — `bookGeneration.ts:2147/2157/2158/2170/2247/2255/2293`. [OBS: missing]
- Stalled-job recovery — `server/app.ts:1140/1186` → `lifecycle.ts:2612/2642/2661`. [STATUS: shipped, 5-min interval]
- GEPA scheduled mutation — `server/app.ts:874` → `gepaCron.ts:61/72/247/315/464` → `promptEvolutionCronDispatch.ts:64/101` → `workers/index.ts:1204`. [Note: composition bypasses harness `:315`]
- Shadow-test conclusion — `server/app.ts:884` → `concludeShadowCron.ts:37/56/79` → `concludeShadowTest.ts:106/129/180/215`.
- BCE mutation family — `server/app.ts:915` → `cronRegistry.ts:39/61/74/87/100` → `bceCronDispatch.ts:78/92/120/140/164/185/206`.
- LLM judge nightly DEAD PATH — `server/app.ts:904` → `cron/index.ts:35/52` → `workers/index.ts:1208/1210` (no judge handler) → `bceCronDispatch.ts:92` skip. [STATUS: broken/dead-path] [OBS: missing]
- Preference drift monthly DEAD PATH — `server/app.ts:935` → `preferenceDriftCron.ts:19/30` → `workers/index.ts:1208/1210` (no handler) → `preferenceDriftService.ts:46/182` unreachable. [STATUS: broken/dead-path] [OBS: missing]
- Chapter genre escalation — `escalateGenreLlm.ts:78/140/167/190/216/236`. [STATUS: shipped, traceGemini]
- Vertical classifier — `verticalClassifierLLM.ts:45/105/178/203/227/246`. [OBS: missing]
- Structural evidence cluster summary — `evidenceClusterSummary.ts:44/86/130/182/205/225`. [OBS: missing]

## Hooks (29 features)

- `useCrossBlockHighlight` — `src/hooks/useCrossBlockHighlight.ts:1/104` — Cross-block highlight sync; [CONTRACT: ambiguous race condition]. [CONSENSUS: 2/4] (GLM + Kimi)
- `useTextHighlights` — `src/hooks/useTextHighlights.ts:1/104` — Core highlight CRUD + rendering. [CONSENSUS: 2/4] (GLM + Kimi)
- `useSampleChapterHarvest` — `src/hooks/useSampleChapterHarvest.ts:1` — Sample chapter variant generation+review. [CONSENSUS: 1/4] (GLM)
- `useBookGenerationJob` — `src/hooks/useBookGenerationJob.ts:1` — Book gen job state tracking. [CONSENSUS: 1/4] (GLM)
- `useBookGenerationSocket` — `src/hooks/useBookGenerationSocket.ts:1` — Real-time WebSocket progress. [CONSENSUS: 1/4] (GLM)
- `usePromptEvolution` — `src/hooks/usePromptEvolution.ts:1` [CONSENSUS: 1/4] (GLM)
- `usePromptSettings` — `src/hooks/usePromptSettings.ts:1` [CONSENSUS: 1/4] (GLM)
- `usePublisherStyleSynthesis` — `src/hooks/usePublisherStyleSynthesis.ts:1` [CONSENSUS: 1/4] (GLM)
- `usePublisherStyles` — `src/hooks/usePublisherStyles.ts:1` [CONSENSUS: 1/4] (GLM)
- `useChatSession` — `src/hooks/useChatSession.ts:1` [CONSENSUS: 1/4] (GLM)
- `useChatStream` — `src/hooks/useChatStream.ts:1` [CONSENSUS: 1/4] (GLM)
- `useHighlights` — `src/hooks/useHighlights.ts:1` [CONSENSUS: 1/4] (GLM)
- `useHighlightManagement` — `src/hooks/useHighlightManagement.ts:1` [CONSENSUS: 1/4] (GLM)
- `useKnowledgeNodes` — `src/hooks/useKnowledgeNodes.ts:1` [CONSENSUS: 1/4] (GLM)
- `useReadingProgress` — `src/hooks/useReadingProgress.ts:1` [CONSENSUS: 1/4] (GLM)
- `useBlockRewrite` — `src/hooks/useBlockRewrite.ts:1` [CONSENSUS: 1/4] (GLM)
- `useEvidenceBank` — `src/hooks/useEvidenceBank.ts:1` [CONSENSUS: 1/4] (GLM)
- `useMemoryCalibration` — `src/hooks/useMemoryCalibration.ts:1` [CONSENSUS: 1/4] (GLM)
- `useBookLanguage` — `src/hooks/useBookLanguage.ts:1` [CONSENSUS: 1/4] (GLM)
- `useChapterZoomSummaries` — `src/hooks/useChapterZoomSummaries.ts:1` [CONSENSUS: 1/4] (GLM)
- `usePDFSearch` — `src/hooks/usePDFSearch.ts:1` [CONSENSUS: 1/4] (GLM)
- `useVisualAnalyses` — `src/hooks/useVisualAnalyses.ts:1` [CONSENSUS: 1/4] (GLM)
- `useSelectionActionDispatch` — `src/hooks/useSelectionActionDispatch.ts:1` [CONSENSUS: 1/4] (GLM)
- `useTextSelection` — `src/hooks/useTextSelection.ts:1` [CONSENSUS: 1/4] (GLM)
- `useTextSelectionToKG` — `src/hooks/useTextSelectionToKG.ts:1` [CONSENSUS: 1/4] (GLM)
- `useBookTOC` — `src/hooks/useBookTOC.ts:1` [CONSENSUS: 1/4] (GLM)
- `useDocumentTranslationSettings` — `src/hooks/useDocumentTranslationSettings.ts:1` [CONSENSUS: 1/4] (GLM)
- `useComposeMachine` / `jobStateMachine` / `skipLogic` — `src/pages/compose/useComposeMachine.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `wizardMachine` / `evidenceGate` / `wizardSteps` — `src/components/libwit/onboarding/wizardMachine.ts:34` [CONSENSUS: 1/4] (MiniMax)

## Shared types & subsystem services (51 features)

### Shared type definitions
- `PromptKey` — `shared/types/promptSettings.ts:520` — 400+ prompt key type union; canonical registry. [CONSENSUS: 2/4] (GLM + CLAUDE.md reference)
- `SampleChapterHarvestTypes` — `shared/types/sampleChapterHarvest.ts:1` — BCF dispatch, section feedback, convergence types. [CONSENSUS: 1/4] (GLM)
- `OnboardingSampleChapterTypes` — `shared/types/onboardingSampleChapter.ts:1` — Wizard state machine, step defs, section spans. [CONSENSUS: 1/4] (GLM)

### GEPA Evolution Subsystem (detailed)
- `EvolutionOrchestrator` — `server/services/promptEvolution/evolutionOrchestrator.ts:1` [CONSENSUS: 1/4] (GLM)
- `GepaRunner` — `gepaRunner.ts:1` [CONSENSUS: 1/4] (GLM)
- `GepaReflector` — `gepaReflector.ts:1` [CONSENSUS: 1/4] (GLM)
- `ConcludeShadowTest` — `concludeShadowTest.ts:1` [CONSENSUS: 2/4] (GLM + Codex flow)
- `ConstructStage` — `constructStage.ts:1` [CONSENSUS: 1/4] (GLM)
- `ValidationPipeline` — `validationPipeline.ts:1` — 4-stage: freeform→Zod→saver-audit→coherence. [CONSENSUS: 1/4] (GLM)
- `AdversarialDrill` — `adversarialDrill.ts:1` [CONSENSUS: 1/4] (GLM)
- `ExecutionFitness` — `executionFitness.ts:1` [CONSENSUS: 1/4] (GLM)
- `FleetOverviewService` — `fleetOverviewService.ts:1` [CONSENSUS: 1/4] (GLM)
- `JudgeRubricService` — `judgeRubricService.ts:1` [CONSENSUS: 1/4] (GLM)
- `MetaJudge` — `metaJudge.ts:1` [CONSENSUS: 1/4] (GLM)
- `ShadowJudge` — `shadowJudge.ts:1` [CONSENSUS: 1/4] (GLM)
- `HistoryCurriculum` — `historyCurriculum.ts:1` [CONSENSUS: 1/4] (GLM)
- `JointMutationRunner` — `jointMutationRunner.ts:1` [CONSENSUS: 1/4] (GLM)
- `PolymorphicPromptAssembly` — `polymorphicPromptAssembly.ts:1` [CONSENSUS: 1/4] (GLM)
- `RollbackGuard` — `rollbackGuard.ts:1` [CONSENSUS: 1/4] (GLM)
- `ClawGuard` L5/L6 — `clawGuardL5.ts:1` + `clawGuardL6.ts:1` [CONSENSUS: 1/4] (GLM)
- `VistaGate` — `vistaGate.ts:1` [CONSENSUS: 1/4] (GLM)
- `SemanticPromptCache` — `semanticPromptCache.ts:1` [CONSENSUS: 1/4] (GLM)
- `InputSecurityScanner` — `inputSecurityScanner.ts:1` [CONSENSUS: 1/4] (GLM)
- `PendingReaperCron` — `pendingReaperCron.ts:1` [CONSENSUS: 1/4] (GLM)
- `ConcludeShadowCron` — `concludeShadowCron.ts:1` [CONSENSUS: 2/4] (GLM + Codex)
- `LlmJudgeCron` — `llmJudgeCron.ts:1` [CONSENSUS: 1/4] (GLM)
- `RedTeamCron` — `redTeamCron.ts:1` [CONSENSUS: 1/4] (GLM)
- `GepaCron` — `gepaCron.ts:1` [CONSENSUS: 2/4] (GLM + Codex)
- `UnattendedRateProbe` — `unattendedRateProbe.ts:1` [CONSENSUS: 1/4] (GLM)
- `BalancedEvaluation` — `balancedEvaluation.ts:1` [CONSENSUS: 1/4] (GLM)
- `KappaDiscount` — `kappaDiscount.ts:1` [CONSENSUS: 1/4] (GLM)
- `LrfAdapter` — `lrfAdapter.ts:1` [CONSENSUS: 1/4] (GLM)
- `LrgaAttributor` — `lrgaAttributor.ts:1` [CONSENSUS: 1/4] (GLM)
- `MolTsAllocator` — `molTsAllocator.ts:1` [CONSENSUS: 1/4] (GLM)
- `PineDebias` — `pineDebias.ts:1` [CONSENSUS: 1/4] (GLM)
- `PipelineProxySignals` — `pipelineProxySignals.ts:1` [CONSENSUS: 1/4] (GLM)
- `PromotionChecklist` — `promotionChecklist.ts:1` [CONSENSUS: 1/4] (GLM)
- `RenderValidityScorer` — `renderValidityScorer.ts:1` [CONSENSUS: 1/4] (GLM)
- `HitlMutationDecision` — `hitlMutationDecision.ts:1` [CONSENSUS: 1/4] (GLM)

### Evidence Provider Registry
- `ArxivProvider` — `server/services/bookGeneration/evidenceProviders/arxivProvider.ts` [CONSENSUS: 1/4] (GLM)
- `WikipediaProvider` — `wikipediaProvider.ts` [CONSENSUS: 1/4] (GLM)
- `WikidataProvider` — `wikidataProvider.ts` [CONSENSUS: 1/4] (GLM)
- `WikisourceProvider` — `wikisourceProvider.ts` [CONSENSUS: 1/4] (GLM)
- `PubmedProvider` — `pubmedProvider.ts` [CONSENSUS: 1/4] (GLM)
- `OpenalexProvider` — `openalexProvider.ts` [CONSENSUS: 1/4] (GLM)
- `BiorxivProvider` — `biorxivProvider.ts` [CONSENSUS: 1/4] (GLM)
- `HathitrustProvider` — `hathitrustProvider.ts` [CONSENSUS: 1/4] (GLM)
- `InternetArchiveProvider` — `internetArchiveProvider.ts` [CONSENSUS: 1/4] (GLM)
- `EuropeanaProvider` — `europeanaProvider.ts` [CONSENSUS: 1/4] (GLM)
- `DplaProvider` — `dplaProvider.ts` [CONSENSUS: 1/4] (GLM)
- `Pg19Provider` — `pg19Provider.ts` — Gated by `FEATURE_PG19_EXEMPLARS`. [CONSENSUS: 2/4] (GLM + MiniMax flag-gated)
- `ChroniclingAmericaProvider` — `chroniclingAmericaProvider.ts` [CONSENSUS: 1/4] (GLM)
- `EvidenceProviderRegistry` — `registry.ts` — Domain-based routing. [CONSENSUS: 1/4] (GLM)
- `DomainDetector` — `domainDetector.ts` [CONSENSUS: 1/4] (GLM)

### Feedback & Preference System
- `UnifiedPromptFeedbackService` — `server/services/feedback/unifiedPromptFeedbackService.ts:1/94/125/148/167/174/185/196/216/232` [CONSENSUS: 2/4] (GLM + Codex)
- `ExplanationFeedbackService` — `explanationFeedbackService.ts:1/47/154` [CONSENSUS: 2/4] (GLM + Codex)
- `TranslationFeedbackService` — `translationFeedbackService.ts:1` [CONSENSUS: 1/4] (GLM)
- `PreferenceDriftService` — `preferenceDriftService.ts:1/46/182` — Unreachable from worker routing (Codex). [CONSENSUS: 2/4] (GLM + Codex)
- `PreferenceDriftCron` — `preferenceDriftCron.ts:1` [CONSENSUS: 2/4] (GLM + Codex)
- `ImplicitSignals` — `implicitSignals.ts:1` [CONSENSUS: 1/4] (GLM)
- `PairSelector` — `pairSelector.ts:1` [CONSENSUS: 1/4] (GLM)
- `TranslationQualityFactor` — `translationQualityFactor.ts:1` [CONSENSUS: 1/4] (GLM)
- `UserQualityFactor` — `userQualityFactor.ts:1` [CONSENSUS: 1/4] (GLM)

### Other backend services (MiniMax-only)
- `bookExportService` — `server/services/bookExport/` — PDF/EPUB export. [CONSENSUS: 1/4] (MiniMax)
- `bibliographyService` — `server/services/bibliographyService.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `bookChapterEmbeddingService` — `bookChapterEmbeddingService.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `bookChapterQualityReviewer` — `bookChapterQualityReviewer.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `bookFactualReviewer` — `bookFactualReviewer.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `bookGlossaryService` — `bookGlossaryService.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `bookGapAnalyzer` — `bookGapAnalyzer.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `bookIdentityAliasService` — `bookIdentityAliasService.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `bookCodeRegistryService` — `bookCodeRegistryService.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `bookDependencyGraphService` — `bookDependencyGraphService.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `bookCapsuleState` — `bookCapsuleState.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `bookGenerationMessageService` — `bookGenerationMessageService.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `bookGenerationJobService` — `bookGenerationJobService.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `bookGenerationQueueService` — `bookGenerationQueueService.ts:1` — Hatchet+BullMQ dispatch. [CONSENSUS: 2/4] (MiniMax + Codex)
- `bookChapterValidator` — `bookChapterValidator.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `blockCategoryBatchService` + `blockCategoryClassifier` — `blockCategoryBatchService.ts:1` + `blockCategoryClassifier.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `blockHintsMatcher` — `blockHintsMatcher.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `blockExpandPlacement` — `blockExpandPlacement.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `blockStyleDirectionService` — `blockStyleDirectionService.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `blockContentReader` + `blockEntityReader` — `blockContentReader.ts:1` + `blockEntityReader.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `blockActionRefinementService` + `blockExplainProfileService` — Refinement pipeline + per-block explain. [CONSENSUS: 2/4] (MiniMax + Codex)
- `blockMigrationService` — `blockMigrationService.ts:1` [CONSENSUS: 2/4] (MiniMax + Kimi)
- `contentArtifacts` + `contentManifest` + `contentSignals` + `contentRenderHints` — Book-level artifact tracking. [CONSENSUS: 1/4] (MiniMax)
- `hypeEnrich` — Enrich book metadata. [CONSENSUS: 1/4] (MiniMax)
- `aiAnnotationService` — `aiAnnotationService.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `audiobookService` — `audiobookService.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `billingService` — `billingService.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `authService` — `authService.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `arxivImport/` + `arxivGemmaService` + `arxivLibwit/` + `arxivOaiService` + `arxivDiscoveryService` + `arxivPersistenceService` — Corpus import + discovery + persistence. [CONSENSUS: 1/4] (MiniMax)
- `blogIntentCompleter` — `blogIntentCompleter.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `bamlPromptExtractor` — `bamlPromptExtractor.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `agentEvents/` + `agentRunStateReader` + `agentRuns/` + `agentTraceLogger` — Agent run telemetry. [CONSENSUS: 2/4] (MiniMax + Kimi for agentTraceLogger)
- `daytona/` sandbox agents — `server/services/daytona/` — Claude Agent SDK runs in Daytona (mandatory). [CONSENSUS: 2/4] (MiniMax + Kimi BaseSandboxAgent)
- `bookCoverService` — `bookCoverService.ts:1` [CONSENSUS: 1/4] (MiniMax)
- `bookInvariants` + `bookMetrics` — Invariant checks + cost/quality metrics. [CONSENSUS: 1/4] (MiniMax)
- `citationService` — Citation CRUD + dedup. [CONSENSUS: 1/4] (MiniMax)
- `contentNodes` service — `server/services/contentNodes.ts` [CONSENSUS: 2/4] (MiniMax + Kimi)
- `approval/` — Gated approval pipeline. [CONSENSUS: 2/4] (MiniMax + frontend approval components)

## Flag-gated features (4 features)

- `FEATURE_PG19_EXEMPLARS` — Gates `pg19-libwit` MCP server (off by default; on enables pre-1920 stylistic exemplars). `server/.env.example`. [CONSENSUS: 2/4] (MiniMax + CLAUDE.md)
- `FEATURE_FLAGS.ONBOARDING_GATE_AT` — Gates anon→auth transition in dashboard wizard. `wizardMachine.ts:34` + `server/config/featureFlags.ts`. [CONSENSUS: 1/4] (MiniMax)
- `FEATURE_*` flags (20+) — Enumerated in `server/config/features.ts` and `featureFlags.ts`; flip via SealedSecret on jisawru. [CONSENSUS: 1/4] (MiniMax)
- `MO_SHOW_BASELINE_TSC_ROT` — Exposes second tsc pass in CI. [CONSENSUS: 1/4] (MiniMax + learning_w3_reviewer_diff_direction_p1)

## Disputed entries

- **Resume-from-checkpoint for chapter generation** — GLM lists `POST /job/:jobId/resume` at `server/routes/bookGeneration.ts:1196` describing "Resume a paused/stuck job from last checkpoint." MiniMax §"Coverage gap report" #1 says "No resume-from-checkpoint for chapter generation — every `chapterGenerationProcessor` invocation runs end-to-end. No `currentChapter` re-entry or partial-merge logic." Resolution: the route exists (GLM right about backend surface) but its underlying semantics are unimplemented at the processor level (MiniMax right about behavior gap). Mark as STATUS: shipped-route + flagged-incomplete-implementation.
- **Admin-side kill switch** — GLM lists `POST /job/:jobId/hatchet-cancel` (`:2346`) and `POST /job/:jobId/hatchet-replay` (`:2368`). MiniMax §"Coverage gap" #2 says "no admin-side kill switch from backoffice UI." Resolution: backend cancel route exists; FE admin UI affordance missing. Mark as STATUS: backend-shipped + admin-FE-missing.
- **`POST /:slug/pause` / `/:slug/resume` for GEPA** — GLM lists both at `promptEvolution.ts:265/289`. MiniMax §"Coverage gap" #7 says "GEPA dashboard is read-mostly... no manual revert-to-prompt-version-N or fork-prompt-lineage affordance." Resolution: pause/resume both shipped; "rollback to version N" and "fork lineage" affordances missing (separate gaps, not contested — both correct).
- **Translation overlay** — GLM lists `ReaderTranslationOverlay` at `src/components/libwit/reader/ReaderTranslationOverlay.tsx:1`. MiniMax §"Coverage gap" #4 says it's "one-shot... no per-paragraph toggle, no show-original-on-hover, no A/B compare." Resolution: component shipped, advanced interaction states missing.

## Coverage gap report

Merged from MiniMax §"Coverage gap report" + Kimi `[CONTRACT: ambiguous]` markers + Codex `[OBS: missing]` and `[STATUS: dead-path]` flags.

### Functional gaps (MiniMax)

1. **No resume-from-checkpoint for chapter generation** — `chapterGenerationProcessor` runs end-to-end. If a 30-chapter job fails at chapter 27, user restarts from chapter 1. `bookGen` family has no `/api/book-generation/:jobId/cancel` or fully-functional `/resume` surface. Workaround: admin-edit BullMQ queue.
2. **No admin-side kill switch for stuck jobs** — `bookGenerationJobService.ts` exists but no `/api/admin/jobs/cancel` route in the admin surface. Workaround: direct BullMQ `redis-cli DEL dev_book-generation:active` (not recommended).
3. **Reader ask-action has no answer-history surface** — `lw-selection-action-prompt` opens panel (`SelectionActionRow.tsx:129`) but no way to revisit past ask-action answers from reader. Memory page stores them but no in-reader "previous Q&A on this passage" view.
4. **Translation overlay is one-shot** — `ReaderTranslationOverlay.tsx` renders translated chapter but no per-paragraph toggle, no show-original-on-hover, no A/B compare. `CompareMode.tsx` exists for chapters but not for translation case.
5. **Mobile audiobook/podcast UX is bare** — `AudiobookPanel.tsx` + `PodcastPanel.tsx` exist but no mobile-specific shell. Reader header overflow points to desktop panel; viewport overflow + scrubber untested on touch.
6. **Style synthesis has no rollback UX** — `StepStyleSynthesis.tsx` persists to `publisher_styles` with `calibration_bucket` overwriting prior; no "view prior versions" / no diff between successive syntheses. DB stores latest, not history.
7. **GEPA dashboard is read-mostly** — Pause/resume work but no manual "revert to prompt version N" or "fork prompt's lineage" affordance. Mis-evolved prompt can only be paused, not rolled back from the dashboard.

### Observability gaps (Codex `[OBS: missing]` — 9 flows)

8. Prompt registry boot sync (`server/app.ts:704/756`) lacks `withFeature`.
9. Generic prompt resolution fallback path (`promptIntegrationService.ts:1091`) returns no execution id and emits no metric on fallback.
10. Feedback-to-refinement analyze+approve chain (`blockActionRefinements.ts:65/103` + `feedbackAnalysisService.ts:174/197`) has only LLM cost tracking — no route-level `withFeature`/`traceGemini`/OTel span.
11. Sample chapter variant generation route (`sampleChapterHarvest.ts:223`) — no route `withFeature` or OTel span (task-level trace carrier only).
12. Sample chapter section feedback route (`sampleChapterHarvest.ts:386`) — no route `withFeature`/`traceGemini`/OTel span.
13. Book draft creation route (`bookGeneration.ts:389`) — no route-level `withFeature`.
14. Plan + chapter steering routes (`bookGeneration.ts:2147/2247`) — no `withFeature`/`traceGemini`/OTel.
15. Vertical classifier path (`verticalClassifierLLM.ts:178/227`) — LLM cost only; no local `withFeature`/`traceGemini`.
16. Structural evidence cluster summary (`evidenceClusterSummary.ts:182/205`) — same gap as #15.

### Dead paths (Codex `[STATUS: dead-path]` — 2 cron handlers)

17. **LLM judge nightly DEAD PATH** — `server/app.ts:904` + `cron/index.ts:35/52` upserts `llm_judge_nightly` (cron `0 2 * * *`) but `workers/index.ts:1208/1210` only routes prompt-evolution + recognized BCE types; `bceCronDispatch.ts:92` skips unknown types. Judge handler is unreachable. No `withFeature`/`traceGemini` span on the reachable path.
18. **Preference drift monthly DEAD PATH** — `server/app.ts:935` + `preferenceDriftCron.ts:19/30` upserts `preference_drift_monthly` (cron `0 5 1 * *`); same routing gap. `preferenceDriftService.ts:46/182` exists but unreachable.

### Contract ambiguities (Kimi `[CONTRACT: ambiguous]` — 11 services)

19. `blockRewriteService.previewRewrite:187` — Fallback HTML on Gemini empty response silently degrades instead of throwing.
20. `blockRewriteService.commitRewrite:312` — No workspace-scoped ownership check beyond `userUuid` match.
21. `bookOrchestrator.runGeneration:104` — Partial success state: `success=true` even if some chapters failed (fail-open per chapter contradicts Zero-Fallback rule).
22. `BaseSandboxAgent.execute:178` — Abstract `getTimeoutMs()` ranges wildly (5min–30min) across subclasses; callers can't predict timeout.
23. `documentUploadPipeline.validateAndStore:94` — Resume mode reuses existing row without validating `workspaceUuid` match (workspace bleed risk).
24. `chatSessionService.sendFirstMessage:312` — `surfaceDepth > cap` is hard-throw but RAG retrieval failure is silent swallow (asymmetric).
25. `promptIntegrationService.resolvePromptForDocument:104` — Silent fallback to base template on ANY error (DB timeout, missing table, corrupt JSON) masks data corruption; no metric emitted.
26. `pipelineExecutor.executeNode:187` — Node failure doesn't halt pipeline unless edge condition depends on it (fail-open per node, fail-closed at pipeline — composes confusingly).
27. `contentNodeService.generateChildContent:312` — Unparseable LLM output logs warning but still inserts blocks with empty content.
28. `entityBlockService.createNoteBlock:104` — Non-transactional by default; caller must pass `PoolClient` but most callers don't, risking partial inserts.
29. `embeddingService.generateEmbeddingsBatch:104` — Empty input returns empty array instead of throwing.

### Test-coverage gaps (Kimi `Tested?: no`)

30. `blockRewriteService` preview + commit — no tests inline.
31. `documentUploadPipeline.validateAndStore` — no tests.
32. `promptIntegrationService.resolvePromptForDocument` — no tests for the production resolution chain.
33. `pipelineOrchestratorService.runPipeline` — no tests for the orchestration layer.
34. `useTextHighlights` hook — no FE tests.
35. `backgroundTaskService.createTask` — no direct tests (only via integration tests).
36. `claudeAgentSdk.query` — no tests for the Claude SDK wrapper.
37. `embeddingService.generateEmbeddingsBatch` — no tests for provider routing or empty-input edge cases.
38. `agentTraceLogger.flush` — no tests for DB-failure or Loki-failure paths.
39. `blockMigrationService.migrateBlocksToChapter` — no tests for collision/cyclic-parent paths.
