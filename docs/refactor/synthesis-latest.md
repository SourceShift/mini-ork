# Feature inventory (synthesis)

## Summary
- Total unique features: 312
- Consensus 4/4: 5
- Consensus 3/4: 22
- Consensus 2/4: 38
- Single-lens finds: 247

---

## Routes / endpoints (66 features)

### GEPA prompt evolution (13)
- `GET /prompt-evolution/test` — `server/routes/promptEvolution.ts:54` — health probe. [CONSENSUS: 1/4] [STATUS: shipped]
- `GET /prompt-evolution/fleet-overview` — `server/routes/promptEvolution.ts:69` — fleet evolution status. Consumed by GepaDashboardPage. [CONSENSUS: 1/4] [STATUS: shipped]
- `GET /prompt-evolution/judge-rubrics` — `server/routes/promptEvolution.ts:93` — list judge rubrics. [CONSENSUS: 1/4] [STATUS: shipped]
- `PATCH /prompt-evolution/judge-rubrics/:prompt_key` — `server/routes/promptEvolution.ts:116` — update rubric weights. [CONSENSUS: 1/4] [STATUS: shipped]
- `GET /prompt-evolution/eligible` — `server/routes/promptEvolution.ts:173` — list evolution-eligible prompts. [CONSENSUS: 1/4] [STATUS: shipped]
- `GET /prompt-evolution/:slug/streak` — `server/routes/promptEvolution.ts:191` — mutation streak status. [CONSENSUS: 1/4]
- `GET /prompt-evolution/:slug/fitness` — `server/routes/promptEvolution.ts:208` — fitness history. [CONSENSUS: 1/4]
- `GET /prompt-evolution/:slug/mutations` — `server/routes/promptEvolution.ts:228` — mutation history. [CONSENSUS: 1/4]
- `POST /prompt-evolution/:slug/pause` — `server/routes/promptEvolution.ts:265` — pause evolution. [CONSENSUS: 1/4]
- `POST /prompt-evolution/:slug/resume` — `server/routes/promptEvolution.ts:289` — resume evolution. [CONSENSUS: 1/4]
- `POST /prompt-evolution/runs/:runId/rollback` — `server/routes/promptEvolution.ts:318` — rollback evolution run. [CONSENSUS: 1/4]
- `POST /prompt-evolution/mutations/:mutationUuid/approve` — `server/routes/promptEvolution.ts:350` — HITL approve. [CONSENSUS: 1/4]
- `POST /prompt-evolution/mutations/:mutationUuid/reject` — `server/routes/promptEvolution.ts:382` — HITL reject. [CONSENSUS: 1/4]

### Prompt feedback (4) [CONSENSUS: 3/4]
- `POST /prompt-feedback/feedback` — `server/routes/promptFeedback.ts:32` — unified accept/reject feedback router by `source`. Wraps `withFeature({name:'prompt-feedback'})`. [CONSENSUS: 3/4] [STATUS: shipped]
- `POST /api/prompts/feedback` (chat client) — `src/services/chatSessionService.ts:388` — posts `source:'chat_message'`. [CONSENSUS: 1/4]
- `POST /api/prompts/feedback` (explanation client) — `src/services/explanationFeedbackService.ts:46` — posts `source:'explanation'`. [CONSENSUS: 1/4]
- `POST /api/prompts/feedback` (block rewrite client) — `src/hooks/useBlockRewrite.ts:868` — posts `source:'block_rewrite'`. [CONSENSUS: 1/4]

### Sample chapter harvest (5) [CONSENSUS: 3/4]
- `POST /sample-chapter-harvest/:sessionId/generate-variants` — `server/routes/sampleChapterHarvest.ts:223` — variant dispatch. [CONSENSUS: 2/4] [STATUS: shipped] [OBS: missing — no withFeature]
- `POST /sample-chapter-harvest/:sessionId/refine` — `server/routes/sampleChapterHarvest.ts:266` — rubric distillation + next iteration. [CONSENSUS: 2/4] [STATUS: shipped] [OBS: missing]
- `PATCH /sample-chapter-harvest/:sessionId/section-feedback` — `server/routes/sampleChapterHarvest.ts:386` — section verdict persist. [CONSENSUS: 2/4] [OBS: missing]
- `GET /sample-chapter-harvest/:sessionId/...` — `server/routes/sampleChapterHarvest.ts:447` — harvest status poll. [CONSENSUS: 1/4]
- `POST /sample-chapter-harvest/*` (compose-surface dispatch+feedback+rubric) — `server/routes/sampleChapterHarvest.ts:223-447` — top-level route group. [CONSENSUS: 1/4]

### Onboarding sample chapter (5) [CONSENSUS: 3/4]
- `POST /onboarding-sample-chapter/` — `server/routes/onboardingSampleChapter.ts:215` — anon+auth dispatch. Wraps `withFeature({name:'onboarding_sample_chapter'})`. [CONSENSUS: 3/4] [STATUS: shipped]
- `GET /onboarding-sample-chapter/status/:jobId` — `server/routes/onboardingSampleChapter.ts:266` — poll generation. [CONSENSUS: 3/4]
- `POST /onboarding-sample-chapter/save` — `server/routes/onboardingSampleChapter.ts:303` — persist variant + section feedback to user profile. [CONSENSUS: 3/4]
- `POST /onboarding-sample-chapter/refresh` — `server/routes/onboardingSampleChapter.ts:400` — drift-aware refresh. Wraps `withFeature({name:'onboarding_sample_chapter_refresh'})`. [CONSENSUS: 2/4]
- Full-book acceptance dispatch — `server/routes/onboardingSampleChapter.ts:360` — `accepted_full_book` branch. [CONSENSUS: 1/4]

### Book generation (4) [CONSENSUS: 4/4]
- `POST /api/book-generation/generate-plan` — `server/routes/bookGeneration.ts:817` — plan-only job creation, async via lifecycle service. Wraps `withFeature({name:'book-generation'})`. [CONSENSUS: 4/4] [STATUS: shipped]
- `POST /api/book-generation/confirm` — `server/routes/bookGeneration.ts` — confirm + enqueue chapter gen. [CONSENSUS: 3/4]
- `POST /api/book-generation/cancel|retry` — `server/routes/bookGeneration.ts` — job lifecycle controls. [CONSENSUS: 1/4]
- `server/routes/bookTreeGeneration.ts` — outline tree gen. Triggered by StepPlan. [CONSENSUS: 2/4]

### Chapter Zoom (3) [CONSENSUS: 2/4]
- `GET /api/chapter-zoom/:chapterUuid/active-task` — `server/routes/chapterZoom.ts:91` — task rehydration. [CONSENSUS: 1/4] [OBS: missing withFeature]
- `POST /api/chapter-zoom/:chapterUuid/regenerate` — `server/routes/chapterZoom.ts:158` — summary (re)generate. [CONSENSUS: 2/4]
- `POST /api/chapter-zoom/:chapterUuid/regenerate/cancel` — `server/routes/chapterZoom.ts:215` — cancel pending/running. [CONSENSUS: 1/4] [OBS: missing]

### Reader / blocks / highlights (14)
- `server/routes/reader/arxivRef.ts` — arXiv reference resolution. [CONSENSUS: 1/4]
- `server/routes/chatSessions.ts` — RAG chat sessions CRUD + list/search/facets/shared/save-to-library. [CONSENSUS: 4/4] [STATUS: shipped]
- `server/routes/highlights.ts` — text highlights CRUD. [CONSENSUS: 1/4]
- `server/routes/textHighlights.ts` — highlight management. [CONSENSUS: 1/4]
- `server/routes/knowledgeNodes.ts` — KG node CRUD. [CONSENSUS: 1/4]
- `server/routes/knowledgeGraph.ts` — KG edge queries. [CONSENSUS: 1/4]
- `server/routes/citations.ts` — citation CRUD. [CONSENSUS: 1/4]
- `server/routes/blockRewrite.ts` — AI block text/image rewrite preview+commit. [CONSENSUS: 3/4] [STATUS: shipped]
- `server/routes/blocks.ts` — block CRUD + mutations. [CONSENSUS: 2/4]
- `server/routes/blockRevisions.ts` — block revision history. [CONSENSUS: 1/4]
- `server/routes/contentNodes.ts` — content node KG bridge. [CONSENSUS: 1/4]
- `server/routes/content*.ts` (anchors / manifest / render-hints / signals) — block-rewrite substrate. [CONSENSUS: 1/4]
- `server/routes/contextEngine.ts` — RAG context builder + sub-question analysis. [CONSENSUS: 1/4]
- `server/routes/conversationTree.ts` — threaded chat tree. [CONSENSUS: 1/4]

### Book ops + ancillary (12)
- `server/routes/bookTranslation.ts` — book clone + translate + RTL/LTR language. [CONSENSUS: 2/4] [STATUS: shipped]
- `server/routes/bookExport.ts` — book HTML/JSON-LD export. [CONSENSUS: 2/4]
- `server/routes/bookSearch.ts` — full-text book search. [CONSENSUS: 2/4]
- `server/routes/bookmarks.ts` — per-block bookmarks. [CONSENSUS: 2/4]
- `server/routes/books.ts` — books CRUD + `POST /:bookUUID/chapters/:n/regenerate` + `re-render`. [CONSENSUS: 1/4]
- `server/routes/bookCovers.ts` — book cover assets. [CONSENSUS: 1/4]
- `server/routes/bookInvariants.ts` — health-check invariants. [CONSENSUS: 1/4]
- `server/routes/bookReadingProgress.ts` — book-level progress. [CONSENSUS: 1/4]
- `server/routes/bookMetrics.ts` — book metrics. [CONSENSUS: 1/4]
- `server/routes/readingProgress.ts` — generic reading progress. [CONSENSUS: 1/4]
- `server/routes/publisherStyles.ts` — style CRUD. [CONSENSUS: 1/4]
- `server/routes/publisherStyleSynthesis.ts` + `publisherStyleSynthesisJob.ts` — synthesis dispatch + status. [CONSENSUS: 1/4]

### Document / research / learning (15)
- `server/routes/documentSummarization.ts` — AI document summary. [CONSENSUS: 1/4]
- `server/routes/deepResearch.ts` — DeerFlow-style deep research dispatcher. [CONSENSUS: 2/4]
- `server/routes/documentResearch.ts` — per-document research queue. [CONSENSUS: 1/4]
- `server/routes/learningQuiz.ts` — quiz generation. [CONSENSUS: 1/4]
- `server/routes/flashcards.ts` — flashcard CRUD. [CONSENSUS: 2/4]
- `server/routes/examQuestions.ts` — exam question generation. [CONSENSUS: 1/4]
- `server/routes/mindmap.ts` — mind map generation. [CONSENSUS: 1/4]
- `server/routes/mindMapReasoning.ts` — KG-backed reasoning mind map. [CONSENSUS: 1/4]
- `server/routes/documentMindMaps.ts` — per-doc mind map. [CONSENSUS: 1/4]
- `server/routes/documentUpload.ts` + `documents.ts` — PDF/MD/CSV upload. [CONSENSUS: 1/4]
- `server/routes/documentsFilter.ts` — full-text + facets across library. [CONSENSUS: 1/4]
- `server/routes/documentExport.ts` — PDF/EPUB/MD export. [CONSENSUS: 1/4]
- `server/routes/documentGCS.ts` + `documentThumbnails.ts` — storage. [CONSENSUS: 1/4]
- `server/routes/documentPromptSettings.ts` + `documentTranslationSettings.ts` — per-doc overrides. [CONSENSUS: 1/4]
- `server/routes/embeddings.ts` — RAG substrate. [CONSENSUS: 1/4]

### Admin / observability (6)
- `GET /api/prompt-executions` (list) — `server/routes/promptExecutions.ts:48` — wraps `withFeature({name:'admin-prompt-executions'})`. [CONSENSUS: 1/4]
- `GET /api/prompt-executions/stats` — `server/routes/promptExecutions.ts:103` — per-prompt-key health. [CONSENSUS: 1/4]
- `GET /api/prompt-executions/observability/summary` — `server/routes/promptExecutions.ts:193` — cross-ledger dashboard. [CONSENSUS: 1/4]
- `POST /api/prompt-executions/compare` — `server/routes/promptExecutions.ts:346` — diff prompt versions. [CONSENSUS: 1/4]
- `server/routes/adminOverview.ts` (`GET /health`) — admin health snapshot. [CONSENSUS: 1/4]
- `server/routes/adminSystem.ts` — canary-stats / trace-quality / failure-genes / circuit-breakers / GEPA trigger / evolution sweep+conclude+stability+eligible / failures stats. [CONSENSUS: 1/4]

### Other operator / API (10)
- `server/routes/auth.ts` — login + magic-link. [CONSENSUS: 1/4]
- `server/routes/workspaces.ts` — workspace members/invitations/billing. [CONSENSUS: 1/4]
- `server/routes/billing.ts` — Stripe + plan surface. [CONSENSUS: 1/4]
- `server/routes/audiobooks.ts` — audiobook gen. [CONSENSUS: 1/4]
- `server/routes/dailyBriefing.ts` — daily email/summary. [CONSENSUS: 1/4]
- `server/routes/backgroundTasks.ts` — operator queue inspection. [CONSENSUS: 1/4]
- `server/routes/debug.ts` + `debugMemoryHealth.ts` — operator-only diag. [CONSENSUS: 1/4]
- `server/routes/agentRuns.ts` — agent run + trajectory inspector. [CONSENSUS: 1/4]
- `server/routes/blog/*` — public blog funnel. [CONSENSUS: 1/4]
- `server/routes/aiAnnotations.ts` — background annotation surface. [CONSENSUS: 1/4]

### Misc routes from MiniMax (15)
- `server/routes/files.ts` — file metadata. [CONSENSUS: 1/4]
- `server/routes/conversations.ts` — chat history. [CONSENSUS: 1/4]
- `server/routes/contextualQuestions.ts` — reader question gen. [CONSENSUS: 1/4]
- `server/routes/crossReferences.ts` — cross-doc refs. [CONSENSUS: 1/4]
- `server/routes/citationExport.ts` — BibTeX/JSON export. [CONSENSUS: 1/4]
- `server/routes/claimBindings.ts` + `claims.ts` — citation provenance. [CONSENSUS: 1/4]
- `server/routes/enhancedNotes.ts` — enhanced notes. [CONSENSUS: 1/4]
- `server/routes/blockExplainProfile.ts` — explain profile per block. [CONSENSUS: 1/4]
- `server/routes/blockActionRefinements.ts` — block action refinement layer. [CONSENSUS: 1/4]
- `server/routes/explainProfile.ts` — explain profile service. [CONSENSUS: 1/4]
- `server/routes/chatGateway.ts` — chat tier-based gateway. [CONSENSUS: 1/4]
- `server/routes/admin/*` (overview / system / debug / debugMemoryHealth) — scoped operational sub-routes. [CONSENSUS: 1/4]
- Public proof page route (`/proofs/:token` consumer) — `src/pages/proofs/PublicProofPage.tsx`. [CONSENSUS: 1/4]
- `server/routes/chatSessions.ts` shared-link `GET /shared/:token` — public chat surface. [CONSENSUS: 1/4]
- Save-chat-to-library `POST /:uuid/messages/:msgUuid/save-to-library` — `server/routes/chatSessions.ts`. [CONSENSUS: 1/4]

---

## React components / pages (118 features)

### Onboarding wizard (15) [CONSENSUS: 3/4 on flow]
- `DashboardWizardSurface` — `src/components/libwit/onboarding/DashboardWizardSurface.tsx:1` — top-level wizard shell. [CONSENSUS: 2/4]
- `OnboardingSurvey` + `OnboardingSurveyLegacy` — `src/components/libwit/onboarding/OnboardingSurvey.tsx:1` — intent survey, secondary path. [CONSENSUS: 2/4]
- `WizardStepper` — `src/components/libwit/onboarding/WizardStepper.tsx:1` — step nav bar. [CONSENSUS: 1/4]
- `TopicPickerStep` — `src/components/libwit/onboarding/TopicPickerStep.tsx:1`. [CONSENSUS: 1/4]
- `SampleChapterVariantCard` — `src/components/libwit/onboarding/SampleChapterVariantCard.tsx:1`. [CONSENSUS: 1/4]
- `EvidenceMeter` — `src/components/libwit/onboarding/EvidenceMeter.tsx:1` — evidence gate indicator. [CONSENSUS: 1/4]
- `SteerSectionPopover` — `src/components/libwit/onboarding/SteerSectionPopover.tsx:1`. [CONSENSUS: 1/4]
- `SaveAndLandingStep` — `src/components/libwit/onboarding/SaveAndLandingStep.tsx:1`. [CONSENSUS: 1/4]
- `VariantGenerationStep` / `VariantReviewStep` — same dir. [CONSENSUS: 1/4]
- `RegisterModal` — `src/components/libwit/onboarding/RegisterModal.tsx:1` + Turnstile verify backend. [CONSENSUS: 2/4]
- `SampleChapterWizard` — `src/components/libwit/onboarding/SampleChapterWizard.tsx:1` — sample co-write. [CONSENSUS: 1/4]
- Step files: `StepIntent`/`StepProfile`/`StepPlan`/`StepSample`/`StepSteer`/`StepClarify`/`StepLanding`/`StepRefreshConfirm`/`StepFullBook` — `src/components/libwit/onboarding/steps/*.tsx:1`. [CONSENSUS: 1/4]
- `wizardMachine` — `src/components/libwit/onboarding/wizardMachine.ts:128` — XState/reducer FSM, 14 actions, pure. [CONSENSUS: 2/4]
- `evidenceGate.dwEvidenceMet` — `src/components/libwit/onboarding/evidenceGate.ts:22` — boolean gate. Partial unit tests. [CONSENSUS: 1/4]
- `OnboardingPage` (anon intent planner) — `src/pages/onboarding/OnboardingPage.tsx:1`. [CONSENSUS: 1/4]

### Compose surface (16) [CONSENSUS: 3/4]
- `ComposePage` — `src/pages/compose/ComposePage.tsx:1` — root wizard. [CONSENSUS: 3/4]
- `useComposeMachine` + `jobStateMachine` + `skipLogic` — `src/pages/compose/`. [CONSENSUS: 1/4]
- `StepTopic` — `src/pages/compose/steps/StepTopic.tsx:1`. [CONSENSUS: 2/4]
- `StepIntent` (compose) — `src/pages/compose/steps/StepIntent.tsx:1`. [CONSENSUS: 1/4]
- `StepConfigure` — `src/pages/compose/steps/StepConfigure.tsx:1`. [CONSENSUS: 2/4]
- `StepPlan` (compose) — `src/pages/compose/steps/StepPlan.tsx:1`. [CONSENSUS: 2/4]
- `StepPlanSketch` — `src/pages/compose/steps/StepPlanSketch.tsx:1`. [CONSENSUS: 2/4]
- `StepVoice` — `src/pages/compose/steps/StepVoice.tsx:1`. [CONSENSUS: 2/4]
- `StepPublisherStyle` — `src/pages/compose/steps/StepPublisherStyle.tsx:1`. [CONSENSUS: 2/4]
- `StepStyleSynthesis` — `src/pages/compose/steps/StepStyleSynthesis.tsx:1`. [CONSENSUS: 2/4]
- `StepStyleBlueprint` — `src/pages/compose/steps/StepStyleBlueprint.tsx:1`. [CONSENSUS: 2/4]
- `StepChapterStyleTuning` — `src/pages/compose/steps/StepChapterStyleTuning.tsx:1`. [CONSENSUS: 2/4]
- `StepRefineCitations` — `src/pages/compose/steps/StepRefineCitations.tsx:1`. [CONSENSUS: 2/4]
- `StepWrite` — `src/pages/compose/steps/StepWrite.tsx:1`. [CONSENSUS: 2/4]
- `ProgressStrip` — `src/pages/compose/ProgressStrip.tsx:1` — chapter FSM. [CONSENSUS: 1/4]
- `ComposePageMobile` — `src/pages/compose/ComposePageMobile.tsx:1`. [CONSENSUS: 1/4]

### Compose sub-components (6)
- `ClaimRefinementPanel` — `src/components/compose/ClaimRefinementPanel.tsx:1`. [CONSENSUS: 1/4]
- `EvidenceBankRail` — `src/components/compose/EvidenceBankRail.tsx:1`. [CONSENSUS: 1/4]
- `BlueprintReaderPreview` — `src/components/compose/blueprint/BlueprintReaderPreview.tsx:116` — markdown render + section overlay. [CONSENSUS: 2/4]
- `BlueprintSectionStrip` — `src/components/compose/blueprint/BlueprintSectionStrip.tsx:1`. [CONSENSUS: 1/4]
- `BlueprintSectionFeedbackPopover` — `src/components/compose/blueprint/BlueprintSectionFeedbackPopover.tsx:1`. [CONSENSUS: 1/4]
- `BlueprintRubricPanel` — `src/components/compose/blueprint/BlueprintRubricPanel.tsx:1`. [CONSENSUS: 1/4]

### Reader chrome (24) [CONSENSUS: 3/4 on reader]
- `ReaderShell` — `src/components/libwit/reader/shell/ReaderShell.tsx:1` — orchestrator. [CONSENSUS: 2/4]
- `ReaderShellAskBar` / `ReaderShellComposer` — inline ask. [CONSENSUS: 1/4]
- `ReaderShellSpine` + `ChapterSpine` — chapter spine rail. [CONSENSUS: 2/4]
- `ReaderShellSessions` + `SessionsPanel` + `FullSessionsBrowser` + `SessionRow` + `SessionsBulkOrchestrator` + `SessionsSearchOrchestrator` — session UI. [CONSENSUS: 1/4]
- `ReaderShellOutlineFromHeadings` — auto outline. [CONSENSUS: 1/4]
- `ReaderShellSummaryPanel` — `src/components/libwit/reader/shell/ReaderShellSummaryPanel.tsx:92` — AI summary panel, polls active-task at mount, dispatches regenerate+cancel. [CONSENSUS: 2/4]
- `ReaderShellViewModePopover` — view mode. [CONSENSUS: 1/4]
- `MarkdownChrome` — `src/components/libwit/reader/MarkdownChrome.tsx:1` — md adapter chrome. [CONSENSUS: 2/4]
- `PdfChrome` — `src/components/libwit/reader/PdfChrome.tsx:1` — pdf chrome. [CONSENSUS: 2/4]
- `MarkdownAdapter` — `src/components/libwit/reader/shell/adapters/MarkdownAdapter.tsx:16` — tracks progress/heartbeat/manifest, handles empty-state + RTL fallback. [CONSENSUS: 2/4]
- `BookToc` — `src/components/libwit/reader/BookToc.tsx:1`. [CONSENSUS: 2/4]
- `ChapterZoomControl` + `ChapterZoomTreeRail` + `ChapterZoomSummaryList` — zoom rail. [CONSENSUS: 2/4]
- `ReaderChatRail` + `ReaderFloatingChat` + `ChatThreadBody` — chat sidebar. [CONSENSUS: 2/4]
- `ReaderHighlightPalette` — highlight color picker. [CONSENSUS: 1/4]
- `ReaderProgressStrip` — `src/components/libwit/reader/ReaderProgressStrip.tsx:1`. [CONSENSUS: 2/4]
- `ReaderTranslationOverlay` — in-place translation. [CONSENSUS: 2/4]
- `CompareMode` + `PdfCompareMode` — side-by-side compare. [CONSENSUS: 2/4]
- `ShareProofButton` — `src/components/libwit/reader/ShareProofButton.tsx:1` — public snippet share. [CONSENSUS: 2/4]
- `ReaderHeader` — top bar. [CONSENSUS: 1/4]
- `ReaderSettingsPopover` — font/theme/layout. [CONSENSUS: 1/4]
- `TimeToMasteryCounter` — reading-stats counter. [CONSENSUS: 1/4]
- `VerifiedLearningCard` — quiz-validated learning card. [CONSENSUS: 1/4]
- `PdfMarginSidebar` + `PdfPageThumbnail` — pdf per-page preview. [CONSENSUS: 1/4]

### Reader mobile (5)
- `MobileReaderChrome` — `src/components/libwit/reader/mobile/MobileReaderChrome.tsx:1`. [CONSENSUS: 1/4]
- `BottomChatSheet` — bottom chat sheet. [CONSENSUS: 1/4]
- `MobileQuizSheet` — mobile quiz. [CONSENSUS: 1/4]
- `MobileCitationCard` / `MobileReaderAskSelectionSheet` / `MobileReaderCiteSheet` / `MobileReaderFindSheet` / `MobileReaderHighlightsSheet` — mobile sheets. [CONSENSUS: 1/4]
- `PageScrubber` — page scrubber control. [CONSENSUS: 1/4]

### PDF search (3)
- `PdfSearchOverlay` — `src/components/libwit/reader/search/PdfSearchOverlay.tsx:1`. [CONSENSUS: 1/4]
- `PdfSearchMinimap` — minimap. [CONSENSUS: 1/4]
- `usePdfSearch` — search hook. [CONSENSUS: 1/4]

### Highlighter / annotation (8) [CONSENSUS: 3/4 on toolbar]
- `HighlighterToolbar` + `HighlighterHoverToolbar` — 7 canonical actions visualize/explain/extend/arxiv/code/rewrite/prompt. [CONSENSUS: 1/4]
- `InlineToolsRow` + `BlockSelectionToolbar` — block-mode toolbar. [CONSENSUS: 1/4]
- `ColorPalette` — category color. [CONSENSUS: 1/4]
- `HighlighterToast` — AI-action status toast. [CONSENSUS: 1/4]
- `useTextHighlights` — CRUD hook with dedup cache. Partial tests. [CONSENSUS: 2/4]
- `useCrossBlockHighlight` — `src/hooks/useCrossBlockHighlight.ts:1` — DOM mouseup, batch INSERT, pipeline dispatch. Has tests in `tests/frontend/hooks/`. [CONSENSUS: 3/4]
- `FeedbackRating` + `FeedbackThumbs` — per-action rating widgets. [CONSENSUS: 1/4]
- `ExplanationFeedbackButtons` — `src/components/blocks/ExplanationFeedbackButtons.tsx:58`. [CONSENSUS: 1/4]

### Chat surface (10)
- `ChatPage` + `PageChat` (notebook per page) + `ChatPageMobile`. [CONSENSUS: 2/4]
- `Composer` (citation-aware) — `src/components/libwit/chat/Composer.tsx:1`. [CONSENSUS: 1/4]
- `AssistantMarkdownContent` — markdown render with citations. [CONSENSUS: 1/4]
- `SourcesRail` + `CitationCard` — source-anchored answers UI. [CONSENSUS: 1/4]
- `FollowupChips` + `ScopeChips` — quick continue / scope narrow. [CONSENSUS: 1/4]
- `RagChatMessage` — `src/features/chat/components/RagChatMessage.tsx:57` — renders `FeedbackThumbs` for non-streaming assistant msgs. [CONSENSUS: 1/4]
- `useChatSession` + `useChatStream` — session + SSE. [CONSENSUS: 1/4]
- `useImplicitSignals` — implicit feedback. [CONSENSUS: 1/4]
- `useDocumentPromptSettings` — per-doc prompt overrides. [CONSENSUS: 1/4]
- `useChapterZoomSummaries` / `useChapterIllustrations` — reader data. [CONSENSUS: 1/4]

### Pages (top-level) (12)
- `TodayPage` — `src/pages/TodayPage.tsx:42` — wizard/dashboard switch via `?wizard=` param. [CONSENSUS: 3/4]
- `TodayPageMobile` — mobile variant. [CONSENSUS: 1/4]
- `LibraryPage` + `LibraryPageMobile` + `Shelf` + `FacetRail` + `SortPopover` + `ContinueHero` + `CoverCard` + `ListRow` — library shelf. [CONSENSUS: 2/4]
- `ReaderPage` + `ReaderPageMobile` + `BookReader` + `BookChapterArticle` — reader entry. [CONSENSUS: 2/4]
- `SearchPage` + `SearchPageMobile` — universal search. [CONSENSUS: 2/4]
- `CitationsPage` + `CitationsPageMobile` — cross-doc citations. [CONSENSUS: 2/4]
- `GraphPage` — KG mindmap. [CONSENSUS: 2/4]
- `ProjectsPage` + `ProjectDetailPage` + `ProjectsPageMobile` — workspace projects. [CONSENSUS: 1/4]
- `WorkspacePage` + `WorkspaceInvitationAcceptPage` — workspace settings. [CONSENSUS: 1/4]
- `MemoryPage` + `MemorySettingsPage` — user memory browser. [CONSENSUS: 1/4]
- `LwSettings/` — per-user prefs. [CONSENSUS: 1/4]
- `LoginPage` + `MagicLinkConsumePage` — auth surface. [CONSENSUS: 1/4]

### Landing / marketing (3)
- `PapersLandingPage` + `FilingsLandingPage` + `PriorArtLandingPage` + `landingConfigs.ts` + `PapersLandingPageMobile`. [CONSENSUS: 1/4]
- `FoundingCheckoutPage` + `FoundingLandingTemplate` — Stripe founder flow. [CONSENSUS: 1/4]
- `PublicProofPage` — public proof token surface. [CONSENSUS: 1/4]

### Admin pages (16) [CONSENSUS: 2/4]
- `GepaDashboardPage` — `src/pages/admin/GepaDashboardPage.tsx:1` + `src/pages/admin/gepa/*` (Btn/FleetTable/FleetRow/VistaDecisionDrawer/PauseModal/ResumeModal/Funnel/Sparkline/SummaryCards/FilterBar/KbdHints/Toast/Pill/StatusGlyph/Head/EmptyFirstRun/EmptyFilter/Error/glossary/fmt/types/gepa.css). [CONSENSUS: 2/4]
- `PromptEvolutionPage` — prompt evolution detail. [CONSENSUS: 2/4]
- `PromptsAdminPage` — CRUD over `prompt_templates`. [CONSENSUS: 1/4]
- `ExecutionLedgerPage` — execution ledger. [CONSENSUS: 2/4]
- `MemoryCalibrationPage` — memory model tuning. [CONSENSUS: 2/4]
- `AgentRunsPage` + `AgentTrajectoryPage` — agent run inspector. [CONSENSUS: 2/4]
- `StyleInspectorPage` — style debug inspector. [CONSENSUS: 2/4]
- `AdminOverviewPage` — health snapshot. [CONSENSUS: 1/4]
- `AdminSystemPage` — system controls. [CONSENSUS: 1/4]
- `FailureAnalyticsPage` + `VerificationHeatmapPage` — failure analytics + heatmap. [CONSENSUS: 1/4]
- `UnattendedRatePage` + `SkillsAnalyticsPage` + `SupportInboxPage` + `WhitelistAdminPage` — operator controls. [CONSENSUS: 1/4]
- `ThemeSettingsPage` — FE theming. [CONSENSUS: 1/4]
- `CompliancePage` — compliance view. [CONSENSUS: 1/4]
- `BookEditorToolsPage` — operator book editor. [CONSENSUS: 1/4]
- `src/pages/dev/SampleChapterHarvestPage.tsx:653` — renders `BlueprintSectionFeedbackPopover`. [CONSENSUS: 1/4]
- `src/pages/dev/*` — internal dev pages. [CONSENSUS: 1/4]

### Dashboard chrome (4)
- `DashboardNavRail` — `src/components/libwit/dashboard/DashboardNavRail.tsx:1` + `Compact.tsx` + `navConfig.ts`. [CONSENSUS: 1/4]
- `GuestSectionPreview` — anon preview. [CONSENSUS: 1/4]
- `WhatsNewDialog` — release notes. [CONSENSUS: 1/4]
- `KeyboardShortcutsDialog` — shortcuts dialog. [CONSENSUS: 1/4]

### Citations / KG components (4)
- `CitationFilterPopover` + `CitationRow` + `BibliographyCard` — citations UI. [CONSENSUS: 1/4]
- `TopConceptsCard` — top concepts. [CONSENSUS: 1/4]
- `NowReadingCard` — Today dashboard tile. [CONSENSUS: 1/4]
- `kgEdgeIndexer` + `kgService.intelligentTopic` — `server/services/kg/*`. [CONSENSUS: 1/4]

---

## Background jobs / workers / cron (49 features)

### BullMQ processors [CONSENSUS: 4/4 on bookGeneration]
- `bookGenerationProcessor` — `server/workers/unified/processors/bookGenerationProcessor.ts:1` — orchestrates full book gen. Queue `book_generation`. [CONSENSUS: 4/4]
- `chapterGenerationProcessor` — `server/workers/unified/processors/chapterGenerationProcessor.ts:45` — Daytona sandbox chapter gen. Idempotency check on existing md children, `withFeature({name:'chapter-generation'})`. Queue `chapter-generation`. [CONSENSUS: 3/4]
- `planGenerationProcessor` — `server/workers/unified/processors/planGenerationProcessor.ts:1`. [CONSENSUS: 1/4]
- `chapterPlanFillProcessor` — orphan chapter plans. [CONSENSUS: 1/4]
- `bookTranslationProcessor` — book translation. [CONSENSUS: 1/4]
- `bookFinalizeProcessor` — book completion + email. [CONSENSUS: 1/4]
- `documentIndexingProcessor` — document embedding + indexing. [CONSENSUS: 1/4]
- `aiAnnotationProcessor` — AI annotation generation. [CONSENSUS: 1/4]
- `hierarchyBuildProcessor` — block hierarchy build. [CONSENSUS: 1/4]
- `examQuestionsProcessor` — exam question gen. [CONSENSUS: 1/4]
- `podcastGenerationProcessor` — podcast TTS gen. [CONSENSUS: 1/4]
- `ttsPreparationProcessor` — TTS chapter extraction. [CONSENSUS: 1/4]
- `artifactGenerationProcessor` — content artifact gen. [CONSENSUS: 1/4]
- `cascadeInvalidationProcessor` — cascade invalidation. [CONSENSUS: 1/4]
- `claimRefinementRegenProcessor` — claim refinement regen. [CONSENSUS: 1/4]
- `chromeLockedProcessor` — chrome-locked processing. [CONSENSUS: 1/4]
- `implicitAcceptanceProcessor` — implicit acceptance. [CONSENSUS: 1/4]
- `preferenceLearningProcessor` — preference learning. [CONSENSUS: 1/4]
- `qualityDriftProcessor` — quality drift detection. [CONSENSUS: 1/4]
- `chapterAbstractProcessor` — chapter abstract generation. [CONSENSUS: 1/4]
- `promptEvolutionCronDispatch` — `server/workers/unified/processors/promptEvolutionCronDispatch.ts:43` — `cronFeatureName='cron:prompt-evolution-dispatch'`; supports `gepa_quarterly`, `conclude_shadow_tests`, `prompt_execution_pending_reaper`. [CONSENSUS: 2/4]
- `arxivHarvestProcessor` — arXiv paper harvesting. [CONSENSUS: 1/4]
- `contextReconcile` — `server/workers/unified/processors/contextReconcile.ts:44` — `cronFeatureName='cron:context_reconcile'`, per-scope reconcile. [CONSENSUS: 2/4]
- `hypeEnrich` — hype enrichment queue. [CONSENSUS: 1/4]
- `chapterZoomRegenerateProcessor` — `server/workers/unified/processors/chapterZoomRegenerateProcessor.ts:35` — summary regen with cancellation sentinel. [CONSENSUS: 1/4]
- `contextFsrsDecay` — `server/workers/unified/processors/contextFsrsDecay.ts:46` — `cronFeatureName='cron:context_fsrs_decay'`, cursor-paginated decay. [CONSENSUS: 1/4]
- `contextCacheReap` — `server/workers/unified/processors/contextCacheReap.ts:35` — `cronFeatureName='cron:context_cache_reap'`; deletes expired `block_context_compose_cache`. [CONSENSUS: 1/4]
- `stuckJobHealthChecker` — `server/workers/unified/processors/stuckJobHealthChecker.ts:51` — `cronFeatureName='cron:stuck-job-health-check'`; finds stale `book_generation_runs`. [CONSENSUS: 1/4]
- `scheduled-pipelines` worker — `server/workers/unified/workers/index.ts:1204` — extracts `pipelineType`, routes prompt-evolution vs BCE. [CONSENSUS: 1/4]
- `addJob` / `addJobToQueue` — `server/workers/unified/queues/index.ts:119` / `:140` / `:248` — OTel carrier injection. [CONSENSUS: 2/4]

### GEPA cron jobs (5)
- `gepaCron` — `server/services/promptEvolution/gepaCron.ts:1`. [CONSENSUS: 1/4]
- `llmJudgeCron` — `server/services/promptEvolution/llmJudgeCron.ts:414` — budget gate; scheduled at `0 2 * * *` via `server/cron/index.ts:35`. [CONSENSUS: 2/4]
- `redTeamCron` — adversarial drill. [CONSENSUS: 1/4]
- `pendingReaperCron` — stale mutation reaper. [CONSENSUS: 1/4]
- `concludeShadowCron` — conclude shadow tests. [CONSENSUS: 1/4]

### Feedback / context cron (3)
- `preferenceDriftCron` — `server/services/feedback/preferenceDriftCron.ts:1`. [CONSENSUS: 1/4]
- `contextReconcileScheduler` — `server/cron/contextReconcileScheduler.ts:43` — adds `context-reconcile-tick`. [CONSENSUS: 1/4]
- `contextFsrsDecayScheduler` + `contextCacheReapScheduler` — `server/cron/contextFsrsDecayScheduler.ts:25` + `server/cron/contextCacheReapScheduler.ts:26`. [CONSENSUS: 1/4]

### Background service jobs (10)
- `backgroundIndexingService` — re-index after edit. [CONSENSUS: 1/4]
- `backgroundTasks/memoryExtractionProcessor` — distill chats → memory. [CONSENSUS: 1/4]
- `backgroundTaskProgress` + `backgroundTaskService` — progress state. [CONSENSUS: 1/4]
- `bookCoverService` — book cover gen. [CONSENSUS: 1/4]
- `bookChapterEmbeddingService` — RAG fuel embeddings. [CONSENSUS: 1/4]
- `bookCompleteEmailer` — user-notification on done. [CONSENSUS: 2/4]
- `chapterAutorubric` + `bookRubric` + `bookFactualReviewer` — quality gate. [CONSENSUS: 1/4]
- `arxivGrowService` + `arxivGemmaService` + `arxivLibwit/` — corpus growth. [CONSENSUS: 1/4]
- `blockClassifier/` + `blockHintsMatcher` — AI hint routing. [CONSENSUS: 1/4]
- `blockMigrationService` — block DB schema migrations. [CONSENSUS: 1/4]
- `spacedRepetitionScheduler` — flashcard timing. [CONSENSUS: 2/4]
- `aiAnnotationWorker` — async annotation pass. [CONSENSUS: 1/4]
- `audiobookService` — audiobook backend. [CONSENSUS: 1/4]
- `anonymousArxivFunnelService` — marketing funnel. [CONSENSUS: 1/4]
- `userChapterProgressService` — user chapter progress. [CONSENSUS: 1/4]
- `bibliographyService` — bibliography. [CONSENSUS: 1/4]
- `agentTraceLogger` + `agentEvents/` + `agentRunStateReader` + `agentRunLinks` — agent observability. [CONSENSUS: 1/4]
- `approval/` — book plan approval flow. [CONSENSUS: 1/4]
- `dailyBriefingService` — daily email/summary. [CONSENSUS: 1/4]

---

## Database tables / migrations (15 features)

- `gepa_mutations` — `server/database/migrations/20260910000000_gepa_mutations_evolution_run_id.sql`. [CONSENSUS: 1/4]
- `evolution_runs` — `server/database/migrations/20260910000001_evolution_run_rollback_support.sql`. [CONSENSUS: 1/4]
- `gepa_mutations_by_vertical` — `server/database/migrations/20260910000002_gepa_mutations_by_vertical.sql`. [CONSENSUS: 1/4]
- `gepa_mutations_hitl` — `server/database/migrations/20260910000003_gepa_mutations_hitl.sql`. [CONSENSUS: 1/4]
- `content_action_anchors` — `server/database/migrations/20260824000000_content_action_anchors.sql`. [CONSENSUS: 1/4]
- `book_steer_events` — `server/database/migrations/20260903000001_world_model_v2_awm_2_simulation_result.sql`. [CONSENSUS: 1/4]
- `book_generation_runs.book_id` (FK) — `server/database/migrations/20260901000000_book_generation_runs_book_id_backfill.sql`. [CONSENSUS: 1/4]
- `verification_results_quiz_item_target` — `server/database/migrations/20260826000000_v4_verification_results_quiz_item_target.sql`. [CONSENSUS: 1/4]
- `explanation_feedback` — written by `unifiedPromptFeedbackService` + `explanationFeedbackService.ts:47`. [CONSENSUS: 2/4]
- `prompt_executions` — pending + outcome rows via `promptExecutionService.createExecution/recordOutcome` (`promptExecutionService.ts:36/97/275`). [CONSENSUS: 2/4]
- `prompt_templates` — registry surface synced from `registerPrompt`. [CONSENSUS: 2/4]
- `block_rewrite_drafts` — written by `blockRewriteService.previewRewrite/commitRewrite/previewImageGen`. [CONSENSUS: 1/4]
- `style_blueprint_samples` + `blueprint_section_feedback` + `blueprint_rubrics` — sample-chapter harvest persistence (`samplePersistence.ts:43/91/147` + `rubricDistiller.ts:190`). [CONSENSUS: 2/4]
- `user_personality` + `personality_pamu_signals` + `personality_atoms` — onboarding profile save (`sampleChapterService.ts:396/417/441/511`). [CONSENSUS: 1/4]
- `rubric_regeneration_log` — `adaRubricService.ts:56` rate-limit check + write. [CONSENSUS: 1/4]
- `block_subtree_summaries` — `chapterZoomService.ts:804` summary insert. [CONSENSUS: 1/4]
- `background_tasks` — `chapterZoomQueueService.ts:76` + various queue facades. [CONSENSUS: 1/4]
- `block_memory_open_set` — context reconcile scope. [CONSENSUS: 1/4]
- `block_context_compose_cache` — context cache reap target. [CONSENSUS: 1/4]
- `llm_api_calls` — LLM cost ledger read by observability summary. [CONSENSUS: 1/4]
- `publisher_styles` — `chapterPlanCalibration.resolvePublisherStyleKeyAsync` queries; `persistJobPublisherStyle` writes `book_generation_runs.publisher_style` + JSONB legacy artifacts. [CONSENSUS: 1/4]

---

## Prompt registry keys (33 features)

### GEPA evolution registry
- Per-book dynamic registration — `server/services/promptEvolution/bookPromptRegistry.ts:1`. [CONSENSUS: 1/4]

### Classification (6)
- `prompt_category_phase1_broad` — `server/services/blockCategoryClassifier.ts:36`. [CONSENSUS: 1/4]
- `prompt_category_phase2_refine` — `:38`. [CONSENSUS: 1/4]
- `prompt_category_phase3_evidence` — `:40`. [CONSENSUS: 1/4]
- `prompt_category_phase4_verify` — `:42`. [CONSENSUS: 1/4]
- `prompt_category_phase4b_escalation` — `:44`. [CONSENSUS: 1/4]
- `prompt_category_batch` — `:46`. [CONSENSUS: 1/4]

### Book generation (8)
- `prompt_plan_fill` — `server/services/book-generation/planFillService.ts:21`. [CONSENSUS: 1/4]
- `prompt_book_chapter_depth` — `server/services/bookGapAnalyzer.ts:16`. [CONSENSUS: 1/4]
- `prompt_book_completeness` — `server/services/bookGapAnalyzer.ts:18`. [CONSENSUS: 1/4]
- `prompt_book_factual_review` — `server/services/bookFactualReviewer.ts:20`. [CONSENSUS: 1/4]
- `prompt_book_structure_validation` — `server/services/bookChapterValidator.ts:26`. [CONSENSUS: 1/4]
- `prompt_book_integrity_validation` — `server/services/bookChapterValidator.ts:27`. [CONSENSUS: 1/4]
- `prompt_illustration_spot` — `server/services/chapterIllustration/spotDetector.ts:15`. [CONSENSUS: 1/4]
- `prompt_narrative_arc` — `server/services/narrativeArcService.ts:34`. [CONSENSUS: 1/4]

### Translation / TTS (3)
- `prompt_book_title_translation` — `server/services/bookTranslation/bookCloneService.ts:25`. [CONSENSUS: 1/4]
- `prompt_tts_chapter_structure` — `server/services/ttsPreparationService.ts:24`. [CONSENSUS: 1/4]
- `prompt_tts_page_convert` — `server/services/ttsPreparationService.ts:26`. [CONSENSUS: 1/4]

### Knowledge graph / content (8)
- `prompt_content_anchor_find` — `server/services/contentNodeService.ts:60`. [CONSENSUS: 1/4]
- `prompt_content_child_relevance` — `server/services/contentNodeService.ts:62`. [CONSENSUS: 1/4]
- `prompt_chapter_suggestions` — `server/services/contentNodeService.ts:64`. [CONSENSUS: 1/4]
- `prompt_content_explain` — `server/services/contentNodeGenerationMixin.ts:27`. [CONSENSUS: 1/4]
- `prompt_content_extend` — `server/services/contentNodeGenerationMixin.ts:29`. [CONSENSUS: 1/4]
- `prompt_content_question` — `server/services/contentNodeGenerationMixin.ts:31`. [CONSENSUS: 1/4]
- `prompt_content_suggest_explorations` — `server/services/contentNodeSuggestionMixin.ts:24`. [CONSENSUS: 1/4]
- `prompt_kg_htc_classify` — `server/services/kgHtc/kgHtcClassifier.ts:31`. [CONSENSUS: 1/4]

### Mind map (3)
- `prompt_mindmap_concept_extract` — `server/services/mindMapReasoningService.ts:22`. [CONSENSUS: 1/4]
- `prompt_mindmap_evidence_analyze` — `:24`. [CONSENSUS: 1/4]
- `prompt_mindmap_answer` — `:26`. [CONSENSUS: 1/4]

### Summarization (3)
- `prompt_annotation_extract` — `server/services/aiAnnotationService.ts:44`. [CONSENSUS: 1/4]
- `prompt_annotation_page_summary` — `:46`. [CONSENSUS: 1/4]
- `prompt_web_article_enhance` — `server/services/webArticleService.ts:27`. [CONSENSUS: 1/4]

### Misc surfaces (4)
- `prompt_rag_chat_response` — chat surface; used by `chatSessionService.sendMessage/streamResponse`. [CONSENSUS: 1/4]
- `prompt_rag_chat_system` — chat system prompt. [CONSENSUS: 1/4]
- `prompt_blueprint_rubric_distiller` — `server/services/sampleChapterHarvest/rubricDistiller.ts:126`. [CONSENSUS: 1/4]
- `prompt_onboarding_full_book_directive` — `server/services/onboarding/fullBookDispatch.ts:50`. [CONSENSUS: 1/4]
- `MARKDOWN_RENDERING_CONTRACT` placeholder — `server/services/promptFragments/outputDirective.ts` — mandatory `outputDirective` for markdown-producing prompts. [CONSENSUS: 1/4]

---

## CLI scripts (3 features)

- `server/routes/admin/*` (adminOverview / adminSystem / debug / debugMemoryHealth) — scoped admin sub-routes serve as operational entry points. [CONSENSUS: 1/4]
- (No first-class CLI one-shots surfaced beyond admin routes.) [CONSENSUS: 1/4]
- `mcp-servers/pg19-libwit/` — local MCP server for PG19 stylistic exemplars (FEATURE_PG19_EXEMPLARS gated). [CONSENSUS: 1/4]

---

## Service-layer features (28 features) [CONSENSUS: 3-4/4 on harness]

### Prompt harness + execution ledger (3)
- `promptIntegrationService.resolvePromptForDocument` — `server/services/promptIntegrationService.ts:508` — 3-tier resolver (document → user → registered default). Reads `prompt_templates`/`user_prompt_settings`/`document_prompt_settings`/`block_action_refinements`; writes `prompt_executions` pending row at `:1059`. Inputs `(userUuid, promptKey, fallbackTemplate, placeholders?, options?)`; output `ResolvedPrompt`. Consumed by every LLM call. [CONSENSUS: 3/4] [STATUS: shipped, partially tested]
- `promptExecutionService.createExecution` / `recordOutcome` — `server/services/promptExecutionService.ts:36/97` — INSERT pending + UPDATE outcome rows. Fire-and-forget from resolver. [CONSENSUS: 2/4]
- `unifiedPromptFeedbackService.recordPromptFeedback` — `server/services/feedback/unifiedPromptFeedbackService.ts:81` — discriminated-union dispatch by `source`; writes `explanation_feedback` + updates `prompt_executions.accepted/user_rating`. Throws `Message not found` / `Draft not found` → 404. [CONSENSUS: 3/4]

### Feedback subsystem (8)
- `explanationFeedbackService.storeFeedback` — `server/services/feedback/explanationFeedbackService.ts:47` — INSERT `explanation_feedback`, implicit-signal normalization, EM/Langfuse/canary side-effects (all caught). [CONSENSUS: 2/4]
- `translationFeedbackService` — translation-specific feedback. [CONSENSUS: 1/4]
- `pairSelector` — feedback pair selection. [CONSENSUS: 1/4]
- `implicitSignals` — implicit signal collection. [CONSENSUS: 1/4]
- `userQualityFactor` — user quality factor. [CONSENSUS: 1/4]
- `translationQualityFactor` — translation QF. [CONSENSUS: 1/4]
- `preferenceDriftService` — drift detection. [CONSENSUS: 1/4]
- `preferenceDriftCron` — drift cron. [CONSENSUS: 1/4]

### Book generation core (15) [CONSENSUS: 4/4]
- `chapterPlannerService.planChapter` — `server/services/bookGeneration/chapterPlannerService.ts` (~line 180+) — Kimi Code Plan API; BAML-typed `ChapterPlan`; zero-fallback (throws on Zod failure or sum-target drift >15%). [CONSENSUS: 3/4]
- `chapterWriterCapsule.executeChapter` — `server/services/bookGeneration/chapterWriterCapsule.ts` (~line 150+) — state machine: draft→critique→revise compound or merged single-agent; rubric eval; mode rollback when quality drops > `ROLLBACK_THRESHOLD=0.15`. [CONSENSUS: 2/4]
- `chapterGenMixin.generateStructuredChapterContent` — `server/services/bookGeneration/chapterGen.ts:110` — Sonnet via `getBookOrchestrator`; `writeChapterFileWithVerification` retries up to `MAX_FILE_WRITE_RETRIES`. [CONSENSUS: 1/4]
- `chapterGenerator` (+ `generateChapterWithRetry`) — `server/services/book-generation/chapterGenerator.ts:665` — wraps in `withFeature({name:'chapter-generation'})`. [CONSENSUS: 1/4]
- `chapterPlanCalibration` constants + `resolvePublisherStyleKeyAsync` — `server/services/bookGeneration/chapterPlanCalibration.ts` — throws `Unknown publisher_style_slug` (zero-fallback). [CONSENSUS: 1/4]
- `canonicalPlanService` + `canonicalChapterSpine` — plan + spine management. [CONSENSUS: 1/4]
- `evidenceBankService` + `evidenceBankMutations` + `evidenceBankReadService` — evidence substrate. [CONSENSUS: 1/4]
- `bookOrchestratorContextFirst` — context-first orchestrator. [CONSENSUS: 1/4]
- `assurancePipeline` — quality assurance pipeline. [CONSENSUS: 1/4]
- `clawGuard` (behavioralAnalyzer + constraintCatalog) — behavioral guard. [CONSENSUS: 1/4]
- `bookMemoryService` — book-scoped memory. [CONSENSUS: 1/4]
- `blindPeerReview` — blind review. [CONSENSUS: 1/4]
- `globalReviewAgent` — global review. [CONSENSUS: 1/4]
- `iMadSelectiveCritic` — selective critic. [CONSENSUS: 1/4]
- `structuralSynthesis` cluster (`citationGraphBuilder` + `leidenClustering` + `multiAspectTaxonomy`). [CONSENSUS: 1/4]
- `learningQuizItemService` — quiz item gen. [CONSENSUS: 1/4]

### GEPA evolution services (28)
- `evolutionOrchestrator` — main GEPA loop. [CONSENSUS: 1/4]
- `gepaRunner` — single run executor. [CONSENSUS: 1/4]
- `gepaReflector` — reflection stage. [CONSENSUS: 1/4]
- `constructStage` — mutation construction. [CONSENSUS: 1/4]
- `validationPipeline` (`stageA_FreeformReasoning` / `stageB_ZodValidation` / `stageC_SaverAudit` / `stageD_CoherenceCheck`). [CONSENSUS: 1/4]
- `calibrationGate` — calibration. [CONSENSUS: 1/4]
- `fixtureRegressionGate` — regression gate. [CONSENSUS: 1/4]
- `rollbackGuard` — rollback guard. [CONSENSUS: 1/4]
- `clawGuardL5` / `clawGuardL6` — constraint guards. [CONSENSUS: 1/4]
- `hitlMutationDecision` — HITL approval flow. [CONSENSUS: 1/4]
- `jointMutationRunner` — joint mutation. [CONSENSUS: 1/4]
- `inputSecurityScanner` — input safety. [CONSENSUS: 1/4]
- `adversarialDrill` + `adaptiveAttackDrill` — adversarial drills. [CONSENSUS: 1/4]
- `metaJudge` + `shadowJudge` — judge meta-eval. [CONSENSUS: 1/4]
- `pineDebias` — position/inversion debias. [CONSENSUS: 1/4]
- `judgeRubricService` — rubric mgmt. [CONSENSUS: 1/4]
- `balancedEvaluation` — balanced eval. [CONSENSUS: 1/4]
- `executionFitness` — fitness scoring. [CONSENSUS: 1/4]
- `renderValidityScorer` — render validity scoring. [CONSENSUS: 1/4]
- `kappaDiscount` — Cohen's kappa discount. [CONSENSUS: 1/4]
- `fleetOverviewService` — fleet overview aggregation. [CONSENSUS: 1/4]
- `semanticPromptCache` — semantic dedup. [CONSENSUS: 1/4]
- `polymorphicPromptAssembly` — polymorphic assembly. [CONSENSUS: 1/4]
- `historyCurriculum` — history-based curriculum. [CONSENSUS: 1/4]
- `lrfAdapter` + `lrgaAttributor` — LRF/LRGa attribution. [CONSENSUS: 1/4]
- `molTsAllocator` — TS allocator. [CONSENSUS: 1/4]
- `vistaGate` — vista gate. [CONSENSUS: 1/4]
- `unattendedRateProbe` + `pipelineProxySignals` + `promotionChecklist` — probes/checklist. [CONSENSUS: 1/4]
- `adaRubricService.triggerRegenerationIfNeeded` — `server/services/promptEvolution/adaRubricService.ts:56` — rate-limited Gemini Flash criterion clarifier; silent-skip on rate limit / missing rubric. [CONSENSUS: 1/4]

### Sample-chapter harvest (3)
- `variantDispatch.dispatchSampleVariants` — `server/services/sampleChapterHarvest/variantDispatch.ts:460` — persistence + classification. [CONSENSUS: 1/4]
- `sectionClassifier.classifySampleSections` — `server/services/sampleChapterHarvest/sectionClassifier.ts:115` — harness-resolved prompt + structured LLM. [CONSENSUS: 1/4]
- `samplePersistence` + `rubricDistiller.refineRubricAndDispatchNext` — `:43/91/145/147` + `rubricDistiller.ts:123/126/139/162/190`. [CONSENSUS: 1/4]

### Onboarding services (3)
- `sampleChapterService.start/save/refresh` — `server/services/onboarding/sampleChapterService.ts:236/247/307/396/417/441/511/660/1023/1041/1070/1085/1095` — anon-job allocation, profile build, traced Gemini enrichment envelope (currently empty), drift score, confirm-or-commit. [CONSENSUS: 1/4]
- `fullBookDispatch` — `server/services/onboarding/fullBookDispatch.ts:46/50/57/66/67` — directive prompt resolve, `StartJobRequest` build, provenance stamp on book run. [CONSENSUS: 1/4]
- `turnstifyVerify` (turnstileVerify.ts) — anon captcha. [CONSENSUS: 1/4]

### Chat / Block rewrite (3)
- `chatSessionService.sendMessage / streamResponse` — `server/services/chatSessionService.ts` (~500+) — RAG `retrieveUserBlocks`, surface-depth cap `SURFACE_DEPTH_CAP=3`, harness `prompt_rag_chat_response`, auto-title on first msg, context compression. [CONSENSUS: 2/4]
- `blockRewriteService.previewRewrite / commitRewrite / previewImageGen` — `server/services/blockRewriteService.ts` (~200+ / 400+ / 600+) — `block_rewrite_drafts`, idempotency hash, `BRIEF_TIMEOUT_MS=60s`, transactional commit + cascade invalidation, optional rate limit via `BLOCK_REWRITE_RATE_LIMIT_ENABLED`. [CONSENSUS: 2/4]
- `persistJobPublisherStyle` — `server/services/publisherStyle/persistJobPublisherStyle.ts:37` — dual-write `book_generation_runs.publisher_style` + `provenance->legacy_job_artifacts->book_plan->publisherStyle` JSONB. [CONSENSUS: 1/4]

### Reader / context (4)
- `chapterZoomService` — `server/services/reader/chapterZoomService.ts:860/1597/804` — wraps in `withFeature`; resolves prompts via harness; writes `block_subtree_summaries`. [CONSENSUS: 1/4]
- `chapterZoomQueueService` — `:76/94/109/124/130/146` — `background_tasks` row + BullMQ enqueue + cancel sentinel. [CONSENSUS: 1/4]
- `reconcileScopeMemory` (called by contextReconcile processor). [CONSENSUS: 1/4]
- `webArticleService` — web article enhance + import. [CONSENSUS: 1/4]

---

## Shared types / hooks (28 features)

### Shared types (4)
- `shared/types/promptSettings.ts:1` — `PromptKey` + `PROMPT_KEYS` + `PromptFeatureArea` + `FEATURE_AREA_DISPLAY_CONFIG`. [CONSENSUS: 1/4]
- `shared/types/sampleChapterHarvest.ts:1` — harvest types. [CONSENSUS: 1/4]
- `shared/types/onboardingSampleChapter.ts:1` — onboarding types. [CONSENSUS: 1/4]
- `shared/types/observability.ts:1` — `FeatureName` union. [CONSENSUS: 1/4]

### Reader hooks (10)
- `useTextHighlights` — `src/hooks/useTextHighlights.ts:1` — CRUD + dedup cache. Partial tests. [CONSENSUS: 2/4]
- `useCrossBlockHighlight` — `src/hooks/useCrossBlockHighlight.ts:1` — batch INSERT + pipeline dispatch. Has tests. [CONSENSUS: 3/4]
- `useTextSelection` — selection state. [CONSENSUS: 1/4]
- `useSelectionActionDispatch` — selection action dispatch (explain/visualize/arxiv/etc). [CONSENSUS: 1/4]
- `useChatSession` + `useChatStream` — chat. [CONSENSUS: 1/4]
- `useCitations` — citations data. [CONSENSUS: 1/4]
- `useReadingProgress` — progress. [CONSENSUS: 1/4]
- `useBookmarks` — bookmarks. [CONSENSUS: 1/4]
- `useKnowledgeNodes` — KG nodes. [CONSENSUS: 1/4]
- `useBlockRewrite` — AI rewrite hook. [CONSENSUS: 1/4]
- `useEvidenceBank` — evidence bank. [CONSENSUS: 1/4]
- `useHighlightPipeline` — highlight AI pipeline. [CONSENSUS: 1/4]
- `useTranslationScrollSync` — translation scroll sync. [CONSENSUS: 1/4]

### Book gen hooks (3)
- `useBookGenerationJob` — job state. [CONSENSUS: 1/4]
- `useBookGenerationSocket` — WS. [CONSENSUS: 1/4]
- `useHatchetBookEvents` — Hatchet stream. [CONSENSUS: 1/4]

### GEPA hooks (8)
- `usePromptEvolution` / `useFleetOverview` / `usePromptExecutions` / `usePromptVersions` / `useJudgeRubrics` / `useExecutionLedger` / `useFailureAnalytics` / `useUnattendedRate`. [CONSENSUS: 1/4]

### Sample chapter hooks (3)
- `useSampleChapterHarvest` — `src/hooks/useSampleChapterHarvest.ts:138/146/151/160/163` — dispatch + poll + variant store. [CONSENSUS: 2/4]
- `usePublisherStyles` — style CRUD. [CONSENSUS: 1/4]
- `usePublisherStyleSynthesis` — synthesis. [CONSENSUS: 1/4]

### Misc hooks (3)
- `useImplicitSignals` / `useMemoryCalibration` / `useMemoryReview` / `useChapterZoomSummaries` / `useChapterIllustrations` / `useDocumentPromptSettings`. [CONSENSUS: 1/4]

### Evidence providers (book gen) (13)
- `arxivProvider` — `server/services/bookGeneration/evidenceProviders/arxivProvider.ts:1`. [CONSENSUS: 1/4]
- `wikipediaProvider`, `wikidataProvider`, `pubmedProvider`, `pg19Provider`, `openalexProvider`, `internetArchiveProvider`, `hathitrustProvider`, `biorxivProvider`, `dplaProvider`, `europeanaProvider`, `wikisourceProvider`, `chroniclingAmericaProvider` — each at corresponding file. [CONSENSUS: 1/4]

---

## Disputed entries

1. **`llm_judge_nightly` scheduled-pipelines routing**
   - Codex: `[OBS: missing] / likely broken dispatch` — registered with `pipelineType:'llm_judge_nightly'` at `server/cron/index.ts:56`, but `server/workers/unified/workers/index.ts:1208-1212` does not route this `pipelineType` to `promptEvolutionCronDispatchProcessor` (only `gepa_quarterly`/`conclude_shadow_tests`/`prompt_execution_pending_reaper`); falls through to BCE.
   - GLM: lists `llmJudgeCron` as a "GEPA cron job, post-mutation" — implies functional.
   - **Verdict** — Codex has the deeper flow trace; suspected broken dispatch path needs verification before any user-visible nightly LLM judge claim. [DISPUTED: Codex vs GLM]

2. **Chapter Zoom active-task + cancel observability**
   - Codex: `[OBS: missing]` on `GET /active-task` route and `POST /cancel` route — no `withFeature` wrapper despite the regenerate path having one.
   - GLM/MiniMax: list as shipped without flagging the gap.
   - **Verdict** — Codex evidence specific (line refs); gap is real but feature surface still ships. [DISPUTED: Codex vs GLM/MiniMax — observability gap]

3. **Sample-chapter harvest route observability**
   - Codex: `[OBS: missing]` on all three sub-routes (`/generate-variants`, `/section-feedback`, `/refine`) — none wrap in `withFeature`; LLM cost tracking is the only observability surface.
   - GLM: lists as shipped.
   - **Verdict** — observability gap is real per Codex line refs at `server/routes/sampleChapterHarvest.ts:223/266/386`. [DISPUTED: Codex vs GLM — observability gap]

4. **Onboarding profile enrichment LLM call**
   - Codex: `sampleChapterService.ts:660` "emits a traced Gemini enrichment envelope, currently with no LLM call" — envelope exists, but call body is unimplemented.
   - Kimi/MiniMax: imply onboarding profile save is fully shipped.
   - **Verdict** — surface shipped, enrichment side-effect is no-op. [DISPUTED: Codex vs others — partial feature]

5. **Wizard step count**
   - GLM: 9 onboarding step files (`StepIntent`/`StepProfile`/`StepPlan`/`StepSample`/`StepSteer`/`StepClarify`/`StepLanding`/`StepRefreshConfirm`/`StepFullBook`).
   - MiniMax: 12-step compose wizard (lists 12 separately for compose; mentions onboarding wizard generally).
   - **Verdict** — different wizards. Onboarding ≠ compose; both are real surfaces. [DISPUTED: GLM vs MiniMax — same name "wizard", different surfaces]

---

## Coverage gap report

### Observability gaps (Codex `[OBS: missing]`)
- `server/routes/sampleChapterHarvest.ts:223` — `POST /:sessionId/generate-variants` lacks `withFeature`.
- `server/routes/sampleChapterHarvest.ts:266` — `POST /:sessionId/refine` lacks `withFeature`.
- `server/routes/sampleChapterHarvest.ts:386` — `PATCH /:sessionId/section-feedback` lacks `withFeature`, span, or `traceGemini`.
- `server/routes/chapterZoom.ts:91` — `GET /active-task` lacks `withFeature`.
- `server/routes/chapterZoom.ts:215` — `POST /regenerate/cancel` lacks `withFeature`.
- `server/workers/unified/workers/index.ts:1208-1212` — `llm_judge_nightly` not routed to `runLlmJudgeCron`; falls through to BCE.

### Test gaps (Kimi `Tested?: no` / `partial`)
- `unifiedPromptFeedbackService.recordPromptFeedback` — no tests.
- `promptExecutionService.createExecution/recordOutcome` — no tests.
- `chapterPlannerService.planChapter` — integration-only.
- `chapterGenMixin.generateStructuredChapterContent` — no tests.
- `persistJobPublisherStyle` — no tests.
- `blockRewriteService.previewRewrite/commitRewrite/previewImageGen` — partial (markdown-to-blocks only).
- `chatSessionService.sendMessage/streamResponse` — partial (mappers only).
- `chapterWriterCapsule.executeChapter` — partial (clawGuardCapsuleBridge extracted; capsule FSM untested).
- `addJob`/`addJobToQueue` — no tests.
- `adaRubricService.triggerRegenerationIfNeeded` — no tests.
- `explanationFeedbackService.storeFeedback` — no tests.
- `wizardMachine.reducer` — partial (only `dwEvidenceMet` truth-table).
- `BlueprintReaderPreview` — no tests.
- `MarkdownAdapter` — no tests.
- `TodayPage` — no tests.
- `chapterPlanCalibration` — no tests.

### Product-surface gaps (MiniMax)
1. **No user-facing chapter/job control panel** — no `POST /:bookUUID/cancel` or `POST /:bookUUID/chapters/:n/cancel` in `server/routes/books.ts`; users who start a 12-chapter book can't kill chapter 7. Closest is admin-only `bookGenerationJobService`.
2. **No user-triggered resume-from-checkpoint** — `chapterResume.ts:1` + `bookResume` infra exists, no UI button. Reader → chapter list has no "Resume from here" CTA.
3. **No public "discuss this passage" surface beyond `ShareProofButton`** — `server/routes/highlights.ts` has no public `/highlights/:id` route.
4. **GEPA evolution invisible to end users** — operator-only `src/pages/admin/GepaDashboardPage.tsx`; no user `/settings/prompt-history` or `RecentPromptChanges` digest.
5. **Audio surface invisible despite backend** — `server/services/audiobookService.ts:1` + `server/routes/audiobooks.ts:1` ship; no `AudioPlayer.tsx` in `src/components/libwit/reader/`, no Play CTA in `ReaderHeader.tsx`/`ChapterSpine.tsx`.
6. **No operator view for cross-block highlight graph health** — FE-only `useCrossBlockHighlight` + `useTextHighlights`; no admin diagnostic in `server/routes/adminSystem.ts`.
7. **Compose re-entry from sidebar restarts at Step 1** — `useComposeMachine.ts` FSM exists; `src/pages/compose/ComposePage.tsx` has no resume-on-mount logic.

### Zero-fallback contracts (Kimi)
- `chapterPlannerService.planChapter` — throws on Zod parse failure / target-char drift > 15%.
- `chapterWriterCapsule.executeChapter` — throws on rubric parse failure / sandbox failure.
- `chapterGenMixin.generateStructuredChapterContent` — throws on LLM or file-write failure.
- `chapterPlanCalibration.resolvePublisherStyleKeyAsync` — throws `Unknown publisher_style_slug`.

### `[STATUS: flag-gated]`
- `PG19 stylistic exemplars` — `FEATURE_PG19_EXEMPLARS=true` off by default. Discoverability invisible (feeds style synthesis only).
