# Research Synthesis — GEPA `promptEvolution/` → Prompt-Harness Migration

**Research question:** Should the `server/services/promptEvolution/` arc (GEPA mutation, reflection, VISTA gates, judge rubric, balanced-eval) migrate onto the mandatory project prompt harness (`registerPrompt` + `resolvePromptForDocument`, 3-tier `document → user → default` override chain)? If so, by what strategy, with what call-site→PromptKey mapping, what cache-key invariant, and what machine-checkable Definition-of-Done?

**Lenses composed:** `lens-glm` (web/practice breadth), `lens-kimi` (academic rigor), `lens-codex` (in-repo + OSS code patterns), `lens-opus` (deep theory/tension). All four read in full.

**Headline:** All four lenses converge that the migration is correct *and* that the naive reading of the kickoff premise is wrong. The kickoff says "43/44 files emit inline prompt literals"; the codex lens, reading live at HEAD, found that **only 10 of 50 non-test files actually call an LLM, 9 of those carry inline literals, and exactly 1 (`adversarialDrill.ts`) already registers** (Codex-A6). The migration backlog is **9 call-sites, not 43 files.** The single most important design finding — agreed by codex and opus, anticipated by glm and kimi as a gap — is that **GEPA mutation *candidates* are data and must NOT get a PromptKey; only the *meta-prompts that drive evolution* (reflector, judge, repair, rubric) are config and migrate.**

---

## Section 1 — TL;DR

- **Migrate — but only the 9 meta-prompt call-sites, never the mutation candidates.** The candidate prompt body flowing through `vistaGate.ts:333` (`scorePrompt(promptBody, …)`) is GEPA *data*; registering it is a category error. *(Codex-A6 + Opus-§3/§7; high.)*
- **Use branch-by-abstraction gated per-file by existing fixtures, not big-bang.** All four refactoring sources reject big-bang; opus prefers strangler-fig but concedes the repo's own per-file fixture gates + pre-prod posture make the call closer than dogma. *(GLM-Fowler + Opus-§4.3/§6 + Kimi-10; high on "not big-bang", medium on strangler-vs-BBA — see §3.)*
- **The cache key for replayed eval/judge prompts must be a full prompt-content hash + version, never owner-keyed or position-truncated.** This is a live defect: `idempotency.ts:44` hashes only the first 200 chars, and GEPA edits are deliberately small/targeted — a mutation past char 200 collides to a stale result. *(Codex-A5 + GLM-PromptLayer-repro + Kimi-8/Kimi-9 + Opus-§4.1; high.)*
- **Treat each eval/judge prompt as a measurement instrument, not a feature.** Judge wording alone shifts outcomes up to 24.2pp; semantically equivalent rewordings flip 25% of verdicts — a drifting un-versioned ruler inside a self-improving loop is a metrology failure. *(Kimi-3/Kimi-4/Kimi-5 + Opus-§1/§4.2; high.)*
- **Definition-of-Done is a machine-checkable invariant cloned from the repo's existing `no-restricted-syntax` gate.** `eslint.config.mjs:333` already bans raw `fetch()` to provider URLs via an AST selector; clone it to flag a literal/template passed as `prompt:` to `llm.generate*` inside `promptEvolution/**`, plus a grep gate in `scripts/lint-critical-gates.sh`. *(Codex-A "invariant" + Opus-§6.4; high.)*

---

## Section 2 — Consensus findings (≥2 lenses converge)

**★★★ (all 4) — A prompt that scores/judges is a versioned measurement instrument, and inline literals are pure liability for it.**
glm frames it as the field's WHY ("prompt versioning must account for non-deterministic LLM outputs", GLM-MLflow; reproducibility needs the exact prompt pinned, GLM-PromptLayer-repro: "69 papers audited, only 5 runnable, 0 fully reproduced"). kimi supplies the effect sizes (Kimi-4/Zhang 2026: judge wording shifts harmful-rate up to **24.2pp**, surface rewording up to **20.1pp**; Kimi-3/Yagubyan 2026: pairwise prefs flip **13.6%** avg, semantically equivalent prompts change majority outcome in **25%** of cases). codex shows the repo *already accepted* a reflective meta-prompt as a harness key (Codex-A3, the `prompt_deep_research_reflect` "P2-10 harness-bypass closure"). opus supplies the theory (Opus-§1: the 2023 LLM-as-judge arrival turned the eval prompt from documentation into an instrument). **This is the load-bearing justification for the entire migration.**

**★★★ (all 4) — GEPA mutation candidates are DATA, not config; only the meta-prompts are config.**
codex makes it concrete and falsifiable: `vistaGate.ts:333` passes the candidate-being-scored as `systemPrompt`; "any ADR recommendation that tries to register `promptBody` is unsound" (Codex-A6). opus elevates it to the central tension and names the carve-out verbatim (Opus-§3/§7: "the candidates flow through placeholders, the instruments flow through the registry"). glm flagged it as a literature gap ("no canonical treatment of self-evolving prompts vs static-config", GLM-gap-1/gap-6). kimi flagged the same as caveat #6 ("GEPA prompts are data, not static config… the harness may need an extension"). **Convergent across breadth, rigor, practice, and theory — the strongest signal in the synthesis.**

**★★★ (all 4) — Cache key for replayed eval prompts must be content-hash + version, not owner-keyed / not truncated.**
codex localizes the bug (Codex-A5: `idempotency.ts:44` = `sha256(...:promptSnippet.slice(0,200))`, 5-min TTL; GEPA edits are "small and targeted" per `gepaReflector.ts:239` → post-char-200 mutation = identical key = stale replay). opus supplies the defense already in-repo (Opus-§4.1/§6.3: `semanticPromptCache.ts:92-93` already does SHA-256 `content_hash` discriminated by `embedding_model_version`) and the CACE framing (Sculley 2015). glm supplies the external recipe (GLM-PromptLayer-repro + GLM-FutureAGI: cache must include prompt content + version + model + temp + seed; GLM-2601.23088 key-collision attack → need ≥SHA-256 collision resistance). kimi supplies the determinism science (Kimi-8/Messina-Scotta 2026: `T_bg ≈ 0.075` even at nominal T=0 → key must include model+provider+version; Kimi-9/Li 2026: 480k calls, temp↔agreement Pearson −0.82 to −0.93 → freeze + record judge temperature).

> **Note — two distinct cache mechanisms, not a contradiction.** codex's `idempotency.ts` (a 5-min request-dedup middleware, truncated/owner-keyed = the *defect*) and opus's `semanticPromptCache.ts` (content-hash eval cache = the *correct* model). The ADR fix is: route eval/judge calls through the content-hash cache (or mark them idempotency-exempt), never let the 200-char middleware key a GEPA eval. See §3 for why this is reconciliation, not dispute.

**★★ (codex + glm) — The universal registry schema is "name + version + label", and the gateway sits downstream of prompt resolution.**
codex: researcher's `PromptKey` + A/B variant + doc/user override mirrors Langfuse `getPrompt(name, version, {label})` (Codex-B1), promptflow filename (Codex-B3), Helicone/PromptLayer (Codex-B6); the `llm.generateStructured` facade takes a raw string, so migration swaps the literal *upstream* of the facade, never touches the facade (Codex-A4/convergent-pattern-2). glm: same triad across Langfuse/LangSmith/Helicone/MLflow (GLM-bucket-1), and prompt-as-flag-payload is a documented rollout pattern (GLM-GrowthBook/FeatBit). **Implication: the researcher harness already IS the convergent shape; the migration just enrolls 9 keys.**

**★★ (opus + kimi) — Provenance must be recorded as a tuple per mutation cycle to defend against judge drift.**
opus: record `(promptKey, source, content_hash, embedding_model_version)` so "by what standard was mutation X promoted?" is one query (Opus-§6.5). kimi: Preference Leakage (Kimi-7/Li 2025: PLS up to **37.9%**, p<0.001) + the alt-annotator test (Kimi-6/Calderon 2025) demand provenance separating judge lineage from generator lineage. RULERS (Kimi-5/Hong 2026) = "locked, versioned, immutable rubric bundles" with QWK 0.7276 vs 0.4319 — the academic blueprint for the registry's judge-rubric keys.

**★★ (glm + kimi) — Co-version the eval set with the prompt; validate each variant before deleting the legacy literal.**
glm: GLM-2601.22025 ("eval harness version-controlled alongside the prompt templates… small enough to run on every change"). kimi: same paper as Kimi-10/Commey 2026 — generic "improved" prompts dropped Llama-3 extraction **100%→90%**, RAG compliance **93.3%→80%**. **Direct support for per-file fixture-gated migration: each harness-registered replacement must pass the existing GEPA fixture before the inline literal is removed.**

**★★ (codex + opus) — A fallback literal survives even in a "no-inline" system; the invariant forbids the *live* literal, not all strings.**
codex-convergent-pattern-3 (researcher `fallbackPrompt`, Langfuse `fallback=`) + Opus-§6.4 carve-out ("forbid literal *templates* while allowing placeholder-injected mutation content"). **The lint gate must be written to this precision or it false-positives on GEPA's raison d'être.**

---

## Section 3 — Disputed findings (sources disagree)

**Dispute 1 — Strangler-fig vs branch-by-abstraction as the chosen strategy.**
- **opus argues strangler-fig** (Opus-§6.1): wrap each call-site behind `resolvePromptForDocument` one file at a time, gated by its fixture, highest-stakes (judge rubric) first. Cites Fowler 2004 + Feathers 2004.
- **opus simultaneously concedes branch-by-abstraction** may win "if the 43 files share so much state that no single file can be migrated without its neighbors" (Opus-§6.1) and that **pre-prod posture makes big-bang itself more tenable than dogma suggests** (Opus-§6.6).
- **glm leans branch-by-abstraction** on structural grounds: "the GEPA migration has no HTTP façade — it's an internal service-layer refactor — so branch-by-abstraction may fit better than strangler-fig" (GLM-bucket-3, citing Fowler's *Patterns of Legacy Displacement* + *LegacySeam*: `resolvePromptForDocument` IS the seam).
- **codex implicitly favors a precedent-extension** (not a named refactor school): replicate the P2-10 closure that already added a reflective key (Codex-A3) — i.e. the pattern is proven in-repo, lowering the risk of any sequencing choice.
- **My judgment:** This is a *scale* disagreement, not a contradiction. Strangler-fig is for replacing a *system* at its edges via a façade; there is no façade here — `resolvePromptForDocument` is an internal **seam** (GLM-LegacySeam), which is the textbook trigger for **branch-by-abstraction**. But the migration is only **9 call-sites**, not 43 files (Codex-A6) — small enough that the strangler/BBA distinction nearly collapses. **Resolution: branch-by-abstraction is the technically precise label (seam, no façade, in-place swap), executed per-file gated by existing fixtures (the strangler discipline opus wants).** The ADR should pick **branch-by-abstraction**, name strangler-fig and big-bang as alternatives-rejected, and note the choice is low-stakes given the 9-site backlog. *Additional evidence that would resolve it fully: the call-site shared-state graph (does any of the 9 sites share mutable module state with another?) — codex's census suggests they don't, but the impl run should confirm before committing to per-file ordering.*

**Dispute 2 — Is the kickoff's "43/44 inline literals" premise accurate?**
- **Planner/kickoff premise:** 43/44 files emit inline literals (audit Bundle A: G-5/G-6/K-9/K-10/D-6/Opus-Seam-A).
- **codex, reading live at HEAD (Codex-A6):** 50 non-test files; **10 call an LLM; 9 carry inline literals; 1 already registers.** "True compliant count is 1 of 50."
- **My judgment:** Not a real contradiction — different denominators. The audit counted *files containing literal strings*; codex counted *files that actually invoke an LLM with a literal as the live prompt*. The migration-relevant number is codex's: **9 call-sites**. The ADR must use 9 (the actionable backlog) and footnote the 43 (the broader literal-sprawl the audit flagged, most of which are non-LLM strings or comments). *Resolving evidence: codex already cites file:line for all 9 (census table) — verifier should confirm each anchor resolves.*

**Dispute 3 — Does the harness override chain *fit* self-evolving prompts at all, or does it need an extension?**
- **kimi (caveat #6) + opus (§3) say the 3-tier chain is INERT for GEPA:** a system-authored prompt has no "user override of default"; forcing `userUuid:"system"` collapses four tiers to default-only ("theater", Opus-§3).
- **opus's conclusion (§6.2) — add a `system`/`evolved` source tier** that bypasses `document→user` resolution; cites that the `source` enum already carries a non-user value (`label`, `promptIntegrationService.ts:181`) proving the tier set is extensible.
- **codex implicitly says no extension needed for the meta-prompts:** they ARE human-authored config (the reflector/judge/repair templates), so the existing default tier fits; only candidates are data, and candidates don't migrate (Codex-A6).
- **My judgment:** Both are right about different objects. **Meta-prompts (the 9 sites) = human-authored config → existing default tier fits, no extension required.** The `system` tier opus proposes is only needed *if* the team ever wants to persist an *evolved* prompt as a reusable canonical default (glm-gap-6's (a)+(b): persist/share mutation output). That is **out of scope for this migration** (which governs instruments, not candidates). **Resolution: the ADR migrates the 9 meta-prompts using the existing 3-tier default; it explicitly defers the `system`/`evolved` tier as a documented future extension, with opus's Kendall-τ experiment (§ below) as the gate for whether it's ever needed.** This is the cleanest reconciliation of the dissent: opus is right the chain is inert for *candidates*, wrong that this blocks *instrument* migration.

> **Per recipe discipline (Nasser 2026): disputes above are reported, not vote-ruled.** Where I render a "resolution" it is a *reconciliation* (the lenses describe different objects/scales), not a majority vote between same-conviction agents.

---

## Section 4 — Cross-lens gaps (in no source)

1. **No canonical "self-evolving prompt vs static-config prompt" treatment.** Every registry vendor (Langfuse/LangSmith/Helicone/MLflow) and academic source assumes human-authored prompts. The GEPA data-vs-config carve-out is constructed from first principles here (GLM-gap-1, Kimi-caveat-6, Opus-§3). *Candidate for: an in-repo ADR contribution + possible upstream note to GEPA authors.*

2. **No published cache-key composition spec for LLM eval replay.** All sources gesture "pin everything"; nobody publishes the canonical recipe. The ADR must *invent* the composite: `sha256(canonical_json{prompt_content, prompt_version, model_id, provider, temperature, seed, parser_schema})` — flagged as an ADR contribution, not established practice (GLM-gap-2, corroborated by Kimi-8/Kimi-9 on which fields are load-bearing).

3. **No strangler-vs-BBA case study for an internal prompt-registry call-pattern migration.** Fowler's canon is application rewrites, not internal registry enrollment (GLM-gap-3). The ADR's branch-by-abstraction choice needs original justification (the seam argument), not just a citation.

4. **No public OSS "no-inline-prompt-literal" lint rule.** The seam canon says "wrap then enforce" but no project publishes the regex/AST rule (GLM-gap-4). The repo's `no-restricted-syntax` clone (Codex-invariant) is novel practice worth documenting.

5. **No third-party empirical data on prompt-regression incidents caused by implicit vs explicit prompt-source.** Vendors *sell* "you need a registry"; the closest evidence (GLM-PromptLayer 69-paper audit) is about academic eval reproducibility, not prompt-source hygiene (GLM-gap-5). **Verifier must not over-claim external validation for the lint-invariant recommendation** — it rests on in-repo reasoning + analogy, not measured incident data.

6. **No accuracy anchor in the judge-reliability literature.** Kimi-caveat-2: Zhang/Yagubyan show prompts *disagree* but rarely establish which is *correct*. **So the migration's honest claim is improved *reproducibility + auditability*, NOT improved accuracy.** The ADR must state this scope limit explicitly.

7. **Open question opus would measure (the OOD experiment):** *Does routing GEPA eval/judge prompts through the harness change the fitness ranking of mutations, holding content byte-identical?* Measurable = Kendall's τ between pre/post-migration mutation orderings. τ≈1.0 → harness is free auditability, migrate. τ<1.0 → harness leaks into the fitness signal (stray placeholder default, Tier-0 A/B firing on a system key, cache-key mismatch) → seal leaks first (Opus-§5). **This is the single best validation experiment for the impl run.**

---

## Section 5 — Numbered recommendations (falsifiable)

1. **Migrate exactly the 9 meta-prompt call-sites** (Codex-A6 census table) into a new `prompt_evolution` feature area, one `PromptKey` each: `prompt_gepa_reflect_root_cause` (`gepaReflector.ts:239`), `prompt_gepa_apply_edit` (`:350`), `prompt_gepa_cron_reflect` (`gepaCron.ts:367`), `prompt_gepa_adversarial_attack` (`adaptiveAttackDrill.ts:149`), `prompt_gepa_rubric_redesign` (`adaRubricService.ts:134`), `prompt_gepa_json_repair` (`stageB_ZodValidation.ts:81`), `prompt_gepa_llm_judge` (`llmJudgeCron.ts`), `prompt_gepa_stage_{a,c,d}` (`evolutionOrchestrator.ts`), `prompt_gepa_meta_judge` (`metaJudge.ts`/`balancedEvaluation.ts:88`). *Support: codex (live anchors).* **Wrong if:** the impl run finds a callsite where the mutation candidate and its measuring instrument are fused in one literal (Opus-§7) — that site must be split before migration.

2. **Do NOT register the mutation candidate at `vistaGate.ts:333`** or any `scorePrompt(promptBody, …)` path — it is GEPA data flowing through a placeholder. *Support: Codex-A6 + Opus-§7 (all-4 consensus).* **Wrong if:** product ever wants per-user GEPA personalization (then candidates become owner-scoped — not the case today).

3. **Choose branch-by-abstraction, executed per-file gated by existing fixtures** (`fixtureRegressionGate.ts`), highest-stakes instrument (judge rubric) first. Name strangler-fig + big-bang as alternatives-rejected. *Support: GLM-LegacySeam (seam, no façade) + Opus-§6.1/§6.6 (fixture discipline) + Kimi-10/GLM-2601.22025 (validate-before-delete).* **Wrong if:** the 9 sites share mutable module state (then a single atomic `PromptSource` seam swap beats per-file) — confirm via call-site graph in the impl run.

4. **Make the eval/judge cache key a full content-hash + version, never owner-keyed or 200-char-truncated.** Route GEPA eval/judge through `semanticPromptCache.ts` (content-hash, already correct) and mark those calls exempt from `idempotency.ts`'s 200-char middleware. *Support: Codex-A5 (the bug) + Opus-§4.1/§6.3 (the in-repo defense) + GLM-2601.23088 (≥SHA-256) + Kimi-8/Kimi-9 (include model+temp).* **Wrong if:** GEPA eval results were ever legitimately user-specific — they measure the prompt, not the user.

5. **Ship the machine-checkable DoD invariant** = clone `eslint.config.mjs:333` (`no-restricted-syntax`) to flag a `Literal`/`TemplateLiteral` passed as the `prompt:` property of an `llm.generate*` CallExpression inside `server/services/promptEvolution/**`, **plus** a grep gate in `scripts/lint-critical-gates.sh`: `! grep -REn 'prompt:\s*` + backtick `|const \w*[Pp]rompt\w* = ` + backtick + `You are' server/services/promptEvolution --include='*.ts' | grep -v '\.test\.'`. Green = zero inline literals remain. *Support: Codex-invariant + Opus-§6.4.* **Wrong if:** written too coarsely — it must forbid literal *templates* while allowing placeholder-injected candidate content (the §2 carve-out), or it false-positives on GEPA's own data path.

6. **Record `(promptKey, source, content_hash, embedding_model_version)` as a provenance tuple per mutation cycle.** *Support: Opus-§6.5 + Kimi-5/Kimi-7 (judge drift/leakage).* **Wrong if:** per-cycle storage cost exceeds audit value — implausible at 5 mutations/cycle (`gepaReflector.ts:24`).

7. **Validate each migrated prompt against its existing fixture before deleting the inline literal; claim reproducibility+auditability, not accuracy.** *Support: Kimi-10/GLM-2601.22025 (validate-before-delete) + Kimi-caveat-2 (no accuracy anchor).* **Wrong if:** a fixture is itself stale/wrong — re-baseline it in the same commit (pre-prod posture permits this).

8. **Run opus's Kendall-τ pass-through experiment in the impl run** as the migration's correctness gate, and **defer the `system`/`evolved` source tier** unless τ<1.0 forces it. *Support: Opus-§5/§6.2 + Kimi-caveat-6.* **Wrong if:** no historical mutation cycles with recorded fitness exist to replay (then fall back to byte-identical-render assertion per call-site).

---

## Section 6 — Source manifest

### lens-glm (web/practice — 22 distinct URLs)
- Langfuse prompt version control — https://langfuse.com/docs/prompt-management/features/prompt-version-control
- LangSmith manage prompts — https://docs.langchain.com/langsmith/manage-prompts
- Helicone prompt mgmt overview/assembly — https://docs.helicone.ai/features/advanced-usage/prompts/overview
- MLflow Prompt Registry — https://mlflow.org/prompt-registry
- PromptLayer "Why LLM Eval Isn't Reproducible" (Feb 2026) — https://blog.promptlayer.com/why-llm-evaluation-results-arent-reproducible-and-what-to-do-about-it
- PromptLayer "LLM Eval Fundamentals" (Jan 2026) — https://blog.promptlayer.com/llm-evaluation-fundamentals-our-guide-for-engineering-teams
- Braintrust "What is prompt versioning" — https://www.braintrust.dev/articles/what-is-prompt-versioning
- Maxim AI "Top 5 Prompt Versioning Tools" — https://www.getmaxim.ai/articles/top-5-prompt-versioning-tools-for-reliable-ai-workflows
- EleutherAI lm-evaluation-harness — https://github.com/EleutherAI/lm-evaluation-harness
- arXiv 2601.22025 "When 'Better' Prompts Hurt" — https://arxiv.org/html/2601.22025v1
- Future AGI "Non-Deterministic LLM Prompts 2026" — https://futureagi.com/blog/non-deterministic-llm-prompts-2025
- DeepEval "Deterministic LLM Eval Metrics" — https://www.confident-ai.com/blog/how-i-built-deterministic-llm-evaluation-metrics-for-deepeval
- Langfuse LLM-as-a-Judge — https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge
- arXiv 2601.23088 "Key Collision Attack on LLM Semantic Caching" — https://arxiv.org/html/2601.23088v1
- Fowler StranglerFigApplication — https://martinfowler.com/bliki/StranglerFigApplication.html
- Fowler "Rewriting Strangler Fig" (2024) — https://martinfowler.com/articles/2024-strangler-fig-rewrite.html
- Fowler "Patterns of Legacy Displacement" — https://martinfowler.com/articles/patterns-legacy-displacement
- Fowler LegacySeam — https://martinfowler.com/bliki/LegacySeam.html
- Hashbyt "Strangler Fig vs Big Bang" — https://medium.com/@hashbyt/strangler-fig-vs-big-bang-which-migration-wins-47d95ab9da60
- Chris Richardson "STOP big bang modernizations" — https://microservices.io/post/architecture/2024/06/27/stop-hurting-yourself-by-doing-big-bang-modernizations.html
- GEPA paper — https://arxiv.org/abs/2507.19457
- gepa-ai/gepa reference impl — https://github.com/gepa-ai/gepa
- LLM Feature Flags in Backends — https://medium.com/@2nick2patel2/llm-feature-flags-in-backends-policy-driven-prompts-and-safe-rollouts-9b8361ca4479
- GrowthBook "Feature flagging AI models" — https://www.growthbook.io/insights/feature-flagging-ai-models-safely-roll-out-changes
- FeatBit "AI Feature Flag Code References" — https://www.featbit.co/blogs/ai-feature-flag-code-references

### lens-kimi (academic — arXiv IDs)
- 2507.19457 GEPA (Agrawal 2025, ICLR 2026 Oral)
- 2508.18870 ReflectivePrompt (Zhuravlev 2025)
- 2606.13685 Coin Flip Judge (Yagubyan 2026)
- 2604.24074 Safety-Benchmark Judge Sensitivity (Zhang 2026, ICIC 2026)
- 2601.08654 RULERS (Hong 2026)
- 2501.10970 Alternative Annotator Test (Calderon 2025, ACL 2025)
- 2502.01534 Preference Leakage (Li 2025, ICLR 2026)
- 2604.22411 Background Temperature (Messina & Scotta 2026, TMLR)
- 2603.28304 Necessity of Setting Temperature in LLM-as-a-Judge (Li 2026)
- 2601.22025 When "Better" Prompts Hurt (Commey 2026)
- 2509.12421 Understanding Prompt Management in GitHub Repos (Li 2025, IEEE Software)
- 2603.15044 Prompt Readiness Levels (Guinard 2026)
- Citation-chain ancestry: 2005.14165, 2107.13586, 2309.08532, 2201.11903, 2310.03714, 2306.05685, 2303.16634, 2305.17926, 2405.01724, 2501.00274, 2404.04475, 2406.11939

### lens-codex (in-repo file:line + OSS repos)
- In-repo: `server/config/promptDecorators.ts:233,50,201`; `server/services/promptIntegrationService.ts:534,560`; `shared/types/promptSettings.ts:552,969,18,113,845,2916`; `server/llm/index.ts:39`; `server/llm/middleware/idempotency.ts:40-44`; `server/services/promptEvolution/gepaReflector.ts:239,350`; `gepaCron.ts:367`; `adaptiveAttackDrill.ts:149`; `adaRubricService.ts:134`; `stageB_ZodValidation.ts:81`; `vistaGate.ts:333`; `adversarialDrill.ts:63`; `eslint.config.mjs:333`
- OSS: github.com/langfuse/langfuse, github.com/promptfoo/promptfoo, github.com/microsoft/promptflow, github.com/BerriAI/litellm, github.com/guidance-ai/guidance, github.com/Helicone/helicone, github.com/eslint/eslint

### lens-opus (theory + in-repo)
- arXiv 2507.19457 (GEPA), 2310.03714 (DSPy), 2306.05685 (Zheng LLM-as-Judge); Sculley 2015 (Hidden Technical Debt / CACE, NeurIPS); Pineau 2021 (Reproducibility checklist); Fowler 2004 (StranglerFig); Feathers 2004 (Working Effectively with Legacy Code)
- In-repo: `server/config/promptDecorators.ts:233`; `server/services/promptIntegrationService.ts:181,507,535`; `server/services/promptEvolution/gepaReflector.ts:2-25`; `server/services/promptEvolution/semanticPromptCache.ts:43,74,92-93`

### synthesis-added
- None beyond the union above. The composite cache-key spec (§4.2) and the branch-by-abstraction seam argument (§3 Dispute 1) are synthesis contributions, not new citations.
