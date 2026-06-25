# Design critique — unified skill registry for book generation

## Research question / deliverable
Critique the design below and return **risks, failure modes, better alternatives, and
what's missing** — NOT an implementation. This is a pre-build architecture review for the
libwit/researcher book-generation system. Be adversarial and concrete.

## Context (the current reality — 3 disconnected systems)
1. **Chat SkillRegistry** (`server/services/skills/skillRegistryService.ts`, DB `skills` table
   + FS `SKILL.md` dirs). Models *agent skills*: `{slug, name, description, allowedTools,
   mcpServers, executionMode: direct_llm|sandbox, sandboxSize, body(prompt), outputType,
   triggerPatterns, visibility, costEstimateUsd}`. Used for reader/highlight actions
   (explain, visualize, arxiv-deep-dive). NOT wired to book chapters.
2. **Fragment Skill Registry** (`server/services/bookGeneration/chapterWritingContract/
   fragmentRegistry.ts`, code-level `FRAGMENTS` const, 22 entries). Models *content/fragment
   skills*: `{id, family: pedagogical|media|formatting|structural|sidecar, construct_slug?,
   emit_form: directive|fenced_code|markdown_image|sidecar|generated, materialization:
   {node_type, renderer}, edges[], status}`. Covers mermaid_diagram, quiz_item, code_block,
   image, viz_image, chapter_illustration, math, etc. Has a render oracle
   (`fragmentRenderOracle.ts`: can-this-render gate) + a learning loop
   (`fragmentLearning*.ts` + PgFragmentRuleStore: learns which fragments work per
   (genre,language), recalls as `fragment_lessons` into the writer prompt).
3. **enrichment_config** (`shared/types/bookEnrichment.ts`, JSONB on book_generation_runs):
   coarse book-level booleans {matplotlib_charts, gemini_images, diagrams_paperbanana,
   review_panel, ...}. Set in compose-wizard StepEnrichmentReview. The chapter-illustration
   pipeline reads ONLY this; it runs POST-chapter-write, decoupled from the writing contract.

Also relevant: `ChapterPlan.fragments?: Partial<Record<BookFragmentKey,boolean>>` is typed +
plumbed to `buildChapterWritingContract` (→ allowed/forbidden constructs) BUT nothing ever
writes it (no plan-gen output, no UI). The chapter-writing contract enforces per-section
`allowed_constructs`/`forbidden_constructs` (ConstructSlug). The illustration trigger maps
enrichment booleans → `allowedVizTypes`.

## Proposed design (4 parts)
**P1 — One registry, common-envelope + `kind` discriminator.** Extend the DB `skills` table
to be the SSOT for BOTH worlds. Shared columns {id, name, description, kind, family,
cost_estimate, badge, visibility, default_on}; kind-specific payload:
`kind='agent_skill'` → {allowedTools, mcpServers, executionMode, sandboxSize, body};
`kind='fragment_skill'` → {emit_form, construct_slug, materialization{node_type,renderer},
edges}. Migrate the code-level FRAGMENTS const into rows. One catalogue API
`GET /api/skills?kind=`.

**P2 — Plan-time auto-assign.** New pass after the outline stabilizes in
iterativeOutlinePlanner (where fragmentSuggestions already plug in): per chapter, an LLM reads
summary+key_concepts, CONSULTS the learned fragment rules (genre,language), and writes
`ChapterPlan.skills[]` (= widen the dormant `.fragments` to include viz/illustration skills).
Auto-assign, learning-informed; user-editable later.

**P3 — Wizard catalogue.** StepEnrichmentReview becomes a real skill catalogue (reads P1 API);
the Plan step gets a per-chapter skill grid showing the auto-assigned skills, editable.
Book-default toggles + per-chapter overrides.

**P4 — Dual enforcement, one plan.** `ChapterPlan.skills` feeds BOTH
buildChapterWritingContract (fragment/construct skills → allowed/forbidden constructs) AND
chapterIllustrationTrigger (viz skills → allowedVizTypes) — coupling the illustration pipeline
to the plan for the first time.

## Specifically critique
- Is the common-envelope + `kind` the right unification, or is forcing agent-skills and
  fragment-skills into one table a category error? Alternatives?
- Migrating a code-level registry (FRAGMENTS, with a render-oracle promotion invariant) into a
  DB table — what breaks? (provenance, the oracle, hot-reload, FS-vs-DB drift like the chat
  registry already has.)
- Auto-assign per chapter: cost, hallucinated skills the renderer can't satisfy, drift vs the
  genre-profile construct allow/forbid lists, interaction with the existing fragment-learning
  loop. Should planning PROPOSE or fully AUTO-assign?
- Dual enforcement: contract (pre-write, per-section constructs) vs illustration (post-write,
  per-chapter viz) operate at different times + granularities. Is one `ChapterPlan.skills`
  vocabulary actually sufficient to drive both, or does the timing/granularity mismatch need
  two projections?
- Sequencing: which part ships first to de-risk? What's the smallest valuable slice?

## Done When
A synthesis doc with: per-part risk table, 2-3 concrete alternative designs for the riskiest
part, a recommended build sequence, and an explicit "what the author is missing" section.
