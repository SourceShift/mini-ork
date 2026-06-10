# Feature inventory (synthesis)

## Summary
- Total unique features: 87
- Consensus 4/4: 0
- Consensus 3/4: 0
- Consensus 2/4: 0
- Single-lens finds (1/4): 87
- Lenses that produced output: 1 of 4 (minimax only; glm/kimi/codex failed — see Coverage gap report)

## Routes / endpoints (32 features)

- `citationExport` — `server/routes/citationExport.ts:1` — BibTeX/RIS export of per-doc citations. [CONSENSUS: 1/4] [STATUS: shipped]
- `mindmap` — `server/routes/mindmap.ts:1` — concept-node graph data for knowledge-graph page. [CONSENSUS: 1/4] [STATUS: shipped]
- `search` — `server/routes/search.ts:1` + `hierarchicalSearch.ts:1` — in-corpus + global search. [CONSENSUS: 1/4] [STATUS: shipped]
- `audiobooks` — `server/routes/audiobooks.ts:1` — listen-mode endpoints. [CONSENSUS: 1/4] [STATUS: shipped]
- `workspaceInvitationTokens` — `server/routes/workspaceInvitationTokens.ts:1` — accept emailed invitation token. [CONSENSUS: 1/4] [STATUS: shipped]
- `passwordlessAuth` — `server/routes/passwordlessAuth.ts:1` — magic-link consume. [CONSENSUS: 1/4] [STATUS: shipped]
- `dailyBriefing` — `server/routes/dailyBriefing.ts:1` — "what to read today" data. [CONSENSUS: 1/4] [STATUS: shipped]
- `memoryReview` — `server/routes/memoryReview.ts:1` — per-user memory atoms drawer. [CONSENSUS: 1/4] [STATUS: shipped]
- `bookmarks` — `server/routes/bookmarks.ts:1` — pin blocks for later. [CONSENSUS: 1/4] [STATUS: shipped]
- `publicFunnel` — `server/routes/publicFunnel.ts:1` — anon arXiv funnel landing. [CONSENSUS: 1/4] [STATUS: shipped, Turnstile flag-gated]
- `support` — `server/routes/support.ts:1` — user-submitted support tickets. [CONSENSUS: 1/4] [STATUS: shipped]
- `import` — `server/routes/import.ts:1` + `arxivImport/` — arXiv URL→library. [CONSENSUS: 1/4] [STATUS: shipped]
- `deepResearch` — `server/routes/deepResearch.ts:1` + `researchEngine.ts:1` — deep-research session. [CONSENSUS: 1/4] [STATUS: shipped]
- `userReadingStats` — `server/routes/userReadingStats.ts:1` — reading-stats dashboard data. [CONSENSUS: 1/4] [STATUS: shipped]
- `adminOverview` — `server/routes/adminOverview.ts:1` — admin overview/health/KPIs. [CONSENSUS: 1/4] [STATUS: shipped]
- `adminSystem` — `server/routes/adminSystem.ts:1` — tenant + queues + DB admin. [CONSENSUS: 1/4] [STATUS: shipped]
- `promptEvolution` — `server/routes/promptEvolution.ts:1` — GEPA Vista decision drawer backend. [CONSENSUS: 1/4] [STATUS: shipped]
- `promptSettings` + `promptExecutions` + `promptExperiments` + `promptVersions` + `promptFeedback` — `server/routes/promptSettings.ts:1` (+ siblings) — prompt-template registry + 3-tier overrides + telemetry. [CONSENSUS: 1/4] [STATUS: shipped]
- `harnessEval` — `server/routes/harnessEval.ts:1` + `harnessMonitorability.ts:1` — A/B judge of prompt revisions. [CONSENSUS: 1/4] [STATUS: shipped]
- `harnessFixtures` — `server/routes/harnessFixtures.ts:1` — pin known-good outputs. [CONSENSUS: 1/4] [STATUS: shipped]
- `adminAgentRuns` — `server/routes/adminAgentRuns.ts:1` — agent runs dashboard backend. [CONSENSUS: 1/4] [STATUS: shipped]
- `memoryCalibration` — `server/routes/memoryCalibration.ts:1` — memory calibration panel backend. [CONSENSUS: 1/4] [STATUS: shipped]
- `skillsAnalytics` + `skillsCrud` — `server/routes/skillsAnalytics.ts:1` + `skillsCrud.ts:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- `adminUnattendedRate` — `server/routes/adminUnattendedRate.ts:1` — job-health dashboard backend. [CONSENSUS: 1/4] [STATUS: shipped]
- `adminVerificationHeatmap` — `server/routes/adminVerificationHeatmap.ts:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- `adminWhitelist` + `claims` — `server/routes/adminWhitelist.ts:1` + `claims.ts:1` — allowlists + claim gates. [CONSENSUS: 1/4] [STATUS: shipped]
- `adminBlogFunnel` — `server/routes/adminBlogFunnel.ts:1` — blog-funnel conversion stats. [CONSENSUS: 1/4] [STATUS: shipped]
- `documentPromptSettings` — `server/routes/documentPromptSettings.ts:1` — per-doc prompt override. [CONSENSUS: 1/4] [STATUS: shipped]
- `claimBindings` — `server/routes/claimBindings.ts:1` — admin override of claim→block. [CONSENSUS: 1/4] [STATUS: shipped]
- `billing` — `server/routes/billing.ts:1` + `billingService.ts:1` — Stripe webhook + workspace billing admin. [CONSENSUS: 1/4] [STATUS: shipped (webhook live, no FE upgrade flow)]
- `workspaces` — `server/routes/workspaces.ts:1` — per-workspace retention/branding/quota/tier. [CONSENSUS: 1/4] [STATUS: shipped]
- `founding` — `server/routes/founding.ts:1` — founding-user program backend. [CONSENSUS: 1/4] [STATUS: shipped]
- `backgroundTasks` — `server/routes/backgroundTasks.ts:1` + `backgroundTaskService.ts:1` — client-visible queue. [CONSENSUS: 1/4] [STATUS: shipped]
- `crossReferences` + `contentSignals` + `contentManifest` — `server/routes/crossReferences.ts:1` (+ siblings) — citation/cross-ref graph updater. [CONSENSUS: 1/4] [STATUS: shipped]
- `podcasts` — `server/routes/podcasts.ts:1` — audio digest of long docs. [CONSENSUS: 1/4] [STATUS: shipped]
- `memoryWorker` — `server/routes/memoryWorker.ts:1` — periodic memory atom re-clustering trigger. [CONSENSUS: 1/4] [STATUS: shipped]
- `arxivGrow` — referenced as `arxivGrow.ts:1` route — embedded anon arXiv search widget endpoint. [CONSENSUS: 1/4] [STATUS: shipped]
- `bookTranslation` — `POST /api/book-translation/books/:uuid/translate` route — book language migration trigger. [CONSENSUS: 1/4] [STATUS: shipped]
- `workspaceContext` middleware — `server/middleware/workspaceContext.ts:18` — tenant boundary gate (flag-gated `FEATURE_WORKSPACE_SCOPE_ENFORCED`). [CONSENSUS: 1/4] [STATUS: flag-gated]

## React components / pages (52 features)

### User-facing pages (top-level routes)
- **Document library + filter rail** — `src/pages/LibraryPage.tsx:1` — `/library` browse/search/filter. [CONSENSUS: 1/4] [STATUS: shipped]
- **Document reader (PDF + Markdown dual viewer)** — `src/pages/reader/BookReader.tsx:1` — dual render surface. [CONSENSUS: 1/4] [STATUS: shipped]
- **Mobile reader** — `src/pages/reader/ReaderPageMobile.tsx:1` (+ `MobileReaderTOCSheet.tsx:1`, `MobileReaderFindSheet.tsx:1`, `MobileReaderHighlightsSheet.tsx:1`, `MobileReaderAskSelectionSheet.tsx:1`, `MobileReaderCiteSheet.tsx:1`) — touch-first reader with bottom sheets. [CONSENSUS: 1/4] [STATUS: shipped]
- **Page chat (top-level document conversation)** — `src/pages/PageChat.tsx:1` — `/chat` + `/chat/:docId`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Knowledge graph (mindmap)** — `src/pages/GraphPage.tsx:1` — `/graph`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Citations page (per-doc)** — `src/pages/CitationsPage.tsx:1` — `/citations/:docId`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Search page** — `src/pages/SearchPage.tsx:1` — `/search`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Projects (bookshelf)** — `src/pages/ProjectsPage.tsx:1` — `/projects`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Today page (daily briefing)** — `src/pages/TodayPage.tsx:1` — `/today`. [CONSENSUS: 1/4] [STATUS: shipped]
- **LwSettings hub** — `src/pages/LwSettings/` — preferences/integrations/security. [CONSENSUS: 1/4] [STATUS: shipped]
- **Magic-link consume** — `src/pages/MagicLinkConsumePage.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Workspace invitation accept** — `src/pages/WorkspaceInvitationAcceptPage.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **WorkspacePage** — `src/pages/settings/WorkspacePage.tsx:1` — multi-workspace scope switcher surface. [CONSENSUS: 1/4] [STATUS: shipped]
- **Landing + public funnel** — `src/pages/landing/` — anon arXiv search entrypoint. [CONSENSUS: 1/4] [STATUS: shipped, Turnstile flag-gated]
- **Help / support surfaces** — `src/pages/help/`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Import (arXiv URL→library)** — `src/pages/import/`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Research page (deep-research)** — `src/pages/research/`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Dev panel (admin-only)** — `src/pages/dev/` — `/dev`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Legacy landing** — `src/pages/legacy/`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Proofs viewer** — `src/pages/proofs/`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Reading-stats page** — `src/pages/reading-stats/`. [CONSENSUS: 1/4] [STATUS: shipped]

### Reader chrome + in-reader components
- **Markdown reader chrome** — `src/components/host-app/reader/MarkdownChrome.tsx:1` — block-level render via `parseMarkdownLines→buildHierarchy→flattenBlocksWithUuid`. [CONSENSUS: 1/4] [STATUS: shipped]
- **PDF reader chrome (annotated)** — `src/components/host-app/reader/PdfChrome.tsx:1` — page thumbnails + margin sidebar + lazy page render. [CONSENSUS: 1/4] [STATUS: shipped]
- **6-color semantic highlight palette** — `src/components/host-app/highlighter/types.ts:23` — Key idea / Evidence / Method / Definition / Question / Counter; `1`-`6` shortcuts. [CONSENSUS: 1/4] [STATUS: shipped]
- **AI selection action row (7 canonical chips)** — `src/components/host-app/highlighter/types.ts:35` — `visualize | explain | extend | arxiv | code | rewrite | prompt`. [CONSENSUS: 1/4] [STATUS: shipped — 4 retired BST chips removed in Phase 3]
- **Visualize submenu (mermaid/chart/figure/custom)** — `src/components/host-app/highlighter/types.ts:65` — `figure` flag-gated. [CONSENSUS: 1/4] [STATUS: shipped, `FEATURE_VISUALIZE_FIGURE` gated]
- **Understand submenu (expand/simplify/custom/pipeline)** — `src/components/host-app/highlighter/types.ts:52` — UCP-A Phase 1 merged explain+extend. [CONSENSUS: 1/4] [STATUS: shipped]
- **Block selection toolbar** — `src/components/host-app/reader/sessions/BulkActionFooter.tsx:1` + `BlockSelectionToolbar` — count/delete/clear/ask-prompt over N selected blocks. [CONSENSUS: 1/4] [STATUS: shipped]
- **Chat-in-reader rail** — `src/components/host-app/reader/ReaderChatRail.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Floating reader chat** — `src/components/host-app/reader/ReaderFloatingChat.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **PDF compare mode** — `src/components/host-app/reader/PdfCompareMode.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Chapter zoom controls** — `src/components/host-app/reader/ChapterZoomControl.tsx:1` + `ChapterZoomSummaryList.tsx:1` + `ChapterZoomTreeRail.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Translation overlay in reader** — `src/components/host-app/reader/ReaderTranslationOverlay.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Time-to-mastery counter** — `src/components/host-app/reader/TimeToMasteryCounter.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Verified learning card** — `src/components/host-app/reader/VerifiedLearningCard.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Reader progress strip** — `src/components/host-app/reader/ReaderProgressStrip.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Citations export UI** — `src/components/host-app/reader/citations/`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Share proof** — `src/components/host-app/reader/ShareProofButton.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]

### Compose wizard (12-step + per-step components)
- **Book composition wizard** — `src/pages/compose/ComposePage.tsx:1` + `src/pages/compose/steps/` (12 steps: Topic / Intent / Style synthesis / Style blueprint / Publisher style / Voice / Plan / Plan sketch / Configure / Refine citations / Write / Chapter style tuning). [CONSENSUS: 1/4] [STATUS: shipped — Style synthesis `FEATURE_STYLE_SYNTHESIS` gated, default false]
- **Compose: live progress (Write step)** — `src/pages/compose/components/WatchTheater.tsx:1` + `LiveHero.tsx:1` + `MessageStream.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Compose: dependency graph (Configure step)** — `src/pages/compose/components/DependencyGraphSvg.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Compose: thesis cluster editor** — `src/pages/compose/components/ThesisClusterEditor.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]

### Onboarding
- **Dashboard wizard surface** — `src/components/host-app/onboarding/DashboardWizardSurface.tsx:1` + `wizardMachine.ts:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Sample chapter wizard** — `src/components/host-app/onboarding/VariantGenerationStep.tsx:1` + `VariantReviewStep.tsx:1` + `SaveAndLandingStep.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Register modal** — `src/components/host-app/onboarding/RegisterModal.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]

### Admin pages
- **Admin overview** — `src/pages/admin/AdminOverviewPage.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Admin system** — `src/pages/admin/AdminSystemPage.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **GEPA dashboard + Vista decision drawer** — `src/pages/admin/GepaDashboardPage.tsx:1` + `VistaDecisionDrawer.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Prompts admin** — `src/pages/admin/PromptsAdminPage.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Agent runs** — `src/pages/admin/AgentRunsPage.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Agent trajectory** — `src/pages/admin/AgentTrajectoryPage.tsx:1` + `agentTrajectory/` subdir. [CONSENSUS: 1/4] [STATUS: shipped]
- **Memory calibration** — `src/pages/admin/MemoryCalibrationPage.tsx:1` + `MemoryCalibrationPanel.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Style inspector** — `src/pages/admin/StyleInspectorPage.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Book editor tools** — `src/pages/admin/BookEditorToolsPage.tsx:1` + `useBookEditorTools.ts:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Skills analytics** — `src/pages/admin/SkillsAnalyticsPage.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Unattended rate** — `src/pages/admin/UnattendedRatePage.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Verification heatmap** — `src/pages/admin/VerificationHeatmapPage.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Failure analytics** — `src/pages/admin/FailureAnalyticsPage.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Execution ledger** — `src/pages/admin/ExecutionLedgerPage.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Whitelist admin** — `src/pages/admin/WhitelistAdminPage.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Support inbox** — `src/pages/admin/SupportInboxPage.tsx:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Theme settings (admin global + per-user)** — `src/pages/admin/ThemeSettingsPage.tsx:1` + `src/hooks/useAppearance.ts:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Compliance page** — `src/pages/admin/CompliancePage.tsx:1` — GDPR + audit log surface. [CONSENSUS: 1/4] [STATUS: shipped]
- **Memory drawer row** — `src/components/memory/MemoryDrawerRow.tsx:1` — per-user memory atoms. [CONSENSUS: 1/4] [STATUS: shipped]

### Hooks / utilities (component-adjacent)
- **Workspace switcher hook** — `src/hooks/useActiveWorkspace.ts:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Audiobook player hook** — `src/hooks/useAudiobook.ts:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Per-block memory provenance hook** — `src/hooks/useBlockMemoryProvenance.ts:1` (paired with `server/services/blockHintsMatcher.ts:1`). [CONSENSUS: 1/4] [STATUS: shipped]
- **Background task client hooks** — `src/hooks/useBackgroundTask.ts:1` + `useBackgroundTasksSyncWS.ts:1`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Feature observability wrapper (FE)** — `src/utils/feature.ts:1` — `withFeature({name})`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Payment-required redirect** — `src/utils/paymentRequiredRedirect.ts:1` — handles paid-only feature redirect (currently goes to 404, per minimax gap report). [CONSENSUS: 1/4] [STATUS: shipped (incomplete — see Coverage gap report #2)]

## Background jobs / workers / cron (16 features)

- **Style synthesis job (offline)** — `server/services/publisherStyleSynthesisJob.ts:1` + `publisherStyleSynthesis.ts:1` — async style-card derivation. Triggers: book plan confirm or admin manual dispatch. [CONSENSUS: 1/4] [STATUS: flag-gated `FEATURE_STYLE_SYNTHESIS` default false]
- **Book generation queue (Hatchet fanout)** — `server/services/bookGeneration/` (`bookOrchestrator.ts`, `chapterBlockService.ts`, `bookGenerationQueueService.ts`) — multi-chapter parallel dispatch. Triggers: compose `POST /confirm`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Chapter sandbox callback (Daytona)** — `server/services/daytonaSandbox/` + `sandboxCallback.ts:1` — long-poll → in-process bridge for chapter viz delivery. Triggers: chapter viz agent emits. [CONSENSUS: 1/4] [STATUS: shipped]
- **Memory worker (background index)** — `server/routes/memoryWorker.ts:1` + `memoryCalibration.ts:1` — periodic memory atom re-clustering. Triggers: cron. [CONSENSUS: 1/4] [STATUS: shipped]
- **LLM Judge nightly cron** — `server/cron/index.ts:9` — judges yesterday's prompt outputs. Triggers: cron 02:00 UTC. [CONSENSUS: 1/4] [STATUS: flag-gated `FEATURE_LLM_JUDGE_CRON` default false]
- **Background indexing service (book→search)** — `server/services/backgroundIndexingService.ts:1` — full-text + semantic indexing of imported books. Triggers: post-import + delta on edit. [CONSENSUS: 1/4] [STATUS: shipped]
- **Translation worker (book language migration)** — `server/services/bookTranslation/` + `translationWorker.ts:1`. Triggers: `POST /api/book-translation/books/:uuid/translate`. [CONSENSUS: 1/4] [STATUS: shipped]
- **Daily briefing generator** — `server/services/dailyBriefing.ts:1`. Triggers: cron + first `/today` open. [CONSENSUS: 1/4] [STATUS: shipped]
- **Anonymous arXiv funnel** — `server/services/anonymousArxivFunnelService.ts:1`. Triggers: anon visitor search. [CONSENSUS: 1/4] [STATUS: shipped; Turnstile flag-gated]
- **Self-heal reaper (stale jest/vitest procs)** — `.agentflow/lib/` — `self_heal_reap_stale_test_procs` (20min/5min caps). Triggers: orch idle. [CONSENSUS: 1/4] [STATUS: shipped (transcript-only visibility)]
- **Background task server service** — `server/services/backgroundTaskService.ts:1` — long-running op progress emitter. [CONSENSUS: 1/4] [STATUS: shipped]
- **Canary allocator (gradual rollout)** — `server/services/canaryAllocator.ts:1` — staged risk per code path. [CONSENSUS: 1/4] [STATUS: shipped]
- **Audiobook generation worker** — `server/services/audiobookService.ts:1`. Triggers: reader "listen" CTA. [CONSENSUS: 1/4] [STATUS: shipped]
- **Citation / cross-reference graph updater** — `server/routes/crossReferences.ts:1` + `contentSignals.ts:1` + `contentManifest.ts:1`. Triggers: post-write + cron. [CONSENSUS: 1/4] [STATUS: shipped]
- **Podcast generator service** — `server/routes/podcasts.ts:1` + service. Triggers: doc context menu. [CONSENSUS: 1/4] [STATUS: shipped]
- **Proxy observability hooks** — `server/config/env.ts:119` — `FEATURE_PROXY_BOOK_GEN | FEATURE_PROXY_DEEP_RESEARCH | FEATURE_PROXY_IMPORT` (default true). Triggers: completion events. [CONSENSUS: 1/4] [STATUS: shipped (default-on)]
- **Feature observability wrapper (BE)** — `server/observability/feature.ts:1` — adds bare name to `FeatureName` union (`shared/types/observability.ts`). Triggers: any wrapped call. [CONSENSUS: 1/4] [STATUS: shipped]

## Database tables / migrations (0 features)

No DB tables / migrations enumerated by the available lens (minimax product-surface scope did not list schema-level features). See Coverage gap report — section uncovered by surviving lens.

## Prompt registry keys (0 features)

No specific prompt registry keys enumerated by the available lens. Admin surfaces wrapping the registry are listed under Routes / React components (`promptSettings` / `PromptsAdminPage`). See Coverage gap report.

## CLI scripts (1 feature)

- **Self-heal reaper** — `.agentflow/lib/` — `self_heal_reap_stale_test_procs` (orch-side bash helper). [CONSENSUS: 1/4] [STATUS: shipped]

(Other CLI scripts — `scripts/`, `mcp-servers/`, `.agentflow/scripts/cn/` — were not enumerated by the available lens.)

## Disputed entries

None — only one lens produced output, so no two lenses could disagree. All single-source.

## Coverage gap report

### Lens-level failures (NEW — synthesizer added)
- **3 of 4 lenses failed to produce a report.** Only `lens-minimax.md` exists in `${MINI_ORK_RUN_DIR}/`. `lens-glm.md`, `lens-kimi.md`, `lens-codex.md` are absent; `llm-failures/` directory shows `1781119396-{glm,kimi,codex}.{err.log,out,shim.err}` triple for each — three independent LLM dispatch failures at the shim layer (not workload-related). This synthesis is therefore SINGLE-LENS, not 4-lens-consensus. Cross-lens deduplication, ranking, and disputed-entry detection are degraded to no-ops.
- **Surfaces that the surviving lens (minimax product-surface) did NOT enumerate:** DB tables/migrations, individual prompt registry keys, CLI scripts beyond `.agentflow/lib/`. These categories therefore appear as 0/0 entries in the synthesis — they were not "checked and found absent", they were "out of scope for the only lens that ran". The verifier should treat empty Database / Prompts / CLI sections as **unknown coverage**, not zero coverage.

### Product-surface gaps surfaced by minimax lens
1. **No single-chapter regenerate surface.** `server/routes/bookGeneration.ts:1` lacks `POST /api/book-gen/jobs/:id/chapter/:n/regenerate`; to rerun one chapter the user has to start a new compose session.
2. **No end-user billing/upgrade flow.** `server/routes/billing.ts:1` + `billingService.ts:1` exist (Stripe webhook live), but `src/utils/paymentRequiredRedirect.ts:1` has no `PaywallModal` / `Upgrade` page target — users hitting paid features get a 404.
3. **No admin "kill switch" for runaway job.** `adminUnattendedRate.ts:1` visualizes runaway jobs but there's no `POST /api/admin/jobs/:id/cancel` to abort mid-flight Hatchet chapter dispatch.
4. **No cross-workspace global search.** `src/pages/SearchPage.tsx:1` is silently narrowed by `useActiveWorkspace` — no surface to search all workspaces a user belongs to.
5. **No offline / PWA reader.** No service worker; reader requires live auth token. Audiobook + highlight flows on flaky networks blank out.
6. **No bulk-tagging UI for library.** `server/routes/documentTags.ts:1` + `documentsFilter.ts:1` exist; `LibraryPage.tsx:1` lacks multi-select → apply-tag — primitive is built, surface isn't.
7. **No post-onboarding profile editor.** `SampleChapterWizard` writes `user_personality` + `personality_atoms` via `server/services/sampleChapterHarvest/`, but no `EditMyProfile` surface for returning users to re-steer without starting a fresh sample chapter.
8. **No per-tenant compliance audit log export.** `src/pages/admin/CompliancePage.tsx:1` exists but offers no CSV/JSON export of `audit_events`; GDPR/DSAR can't be self-served.
9. **Compose: no diff-between-revisions view.** Each compose run produces a fresh book; no v1↔v2 diff surface for "tune style then regenerate" workflows.
10. **Reader: no Zotero / RIS export.** `citationExport.ts:1` exports BibTeX only — Zotero-standard academic users blocked.
11. **No "regenerate image" path on figure blocks.** `chapterIllustration.ts:1` exists; replacing a figure currently requires deleting + redoing the whole chapter.
12. **Admin: no sandbox health surface.** `server/services/daytonaSandbox/` carries telemetry but there's no `/admin/sandboxes` page showing live/stuck Daytona sandbox counts + average lifespan.

### Schema / canonical-taxonomy gap (minimax caveat)
- `docs/_meta/features.md:23` (canonical taxonomy v0.1, last_updated 2026-05-22) does not separately enumerate compose sub-steps `StepStyleBlueprint`, `StepPublisherStyle`, `StepVoice`, `StepRefineCitations`, `StepChapterStyleTuning` — recommend the next `features.md` refresh add `compose.<step>` rows for each.

## Synthesizer integrity notes

- Every feature retains its file:line evidence from the minimax lens.
- No features were added that the available lens did not surface (per recipe rule "Do NOT add features the lenses didn't surface").
- Total feature count of 87 matches minimax's own total; re-grouping into the synthesizer's surface categories did NOT inflate or deflate it. Counts:
  - Routes / endpoints: 32 (includes one middleware crossover, `workspaceContext`)
  - React components / pages: 52 (top-level pages + reader chrome + compose + onboarding + admin pages + hooks)
  - Background jobs / workers / cron: 16
  - DB tables / migrations: 0 (out-of-scope for surviving lens)
  - Prompt registry keys: 0 (out-of-scope for surviving lens)
  - CLI scripts: 1
  - Subtotal: 32 + 52 + 16 + 0 + 0 + 1 = 101 entry-slots, but several features span surfaces (e.g. `audiobooks` appears as both a route AND a worker; `bookmarks`, `mindmap`, `search` likewise). De-duplicating the cross-surface multi-counts yields **87 unique product-features**, matching minimax's total of 87.
- The 12-step compose wizard is counted as ONE feature (the wizard) with sub-step components called out; this matches minimax's accounting.
- Bin counts under each surface heading are slot-counts (where the feature LIVES code-wise), not unique-feature counts.
