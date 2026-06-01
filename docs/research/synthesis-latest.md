# Synthesis — Heterogeneity Precondition in Multi-Agent LLM Review

**Run:** run-1780304502-74061
**Date:** 2026-06-01
**Lens inputs:** `lens-glm.md` (BREADTH, 25 web sources), `lens-kimi.md` (RIGOR, 12 academic papers), `lens-codex.md` (PRACTICE, 12 shipped repos), `lens-opus.md` (THEORY, narrative + 7 anchor citations)

**Research question:** Is the heterogeneity precondition in multi-agent LLM review causally load-bearing (per Rajan 2025 submodularity proof and Nasser 2026 evaluative-fingerprints), or does scaling agent count alone yield equivalent gains?

**Heterogeneity definition (normalized across lenses):** distinct pretraining lineages / model families (GPT vs. Claude vs. Gemini vs. Llama), NOT prompt-, temperature-, or persona-level variation within a single base model. All 4 lenses adopted this definition explicitly. Where sources use a weaker definition, lenses flagged it.

---

## Section 1: TL;DR (≤ 5 bullets)

- **Family-level heterogeneity is strongly associated with — but not proven necessary for — gains in multi-agent LLM review panels.** Five empirical papers show monotonic improvement with added model families; one formal proof (Yang 2026, arxiv:2602.03794) establishes that homogeneous scaling provably saturates. *(Kimi-9 + GLM-01 + Opus §2; **high confidence** on association, **medium** on necessity.)*
- **The plan's anchor citation Rajan 2025 (submodularity proof) could not be located by any lens.** Five targeted searches in lens-kimi returned zero matches; lens-glm and lens-opus both flag it as unverified. Treat the formal-proof framing as **unconfirmed** until the citation is independently produced. *(Kimi anchor section + GLM-25 + Opus footnote 5; **low confidence in citation, high confidence the gap is real**.)*
- **Nasser 2026 (arxiv:2601.05114) is a real preprint — single-author, arXiv-only, no peer review, no independent replication.** Its core finding (Krippendorff α = 0.042 across judges; 77.1% judge-ID accuracy from rubric scores alone) provides mechanistic grounding for heterogeneity but rests on N=3,240 from one author. *(Kimi-8 + GLM-02; **medium confidence**.)*
- **Shipped code overwhelmingly defaults to homogeneous panels.** 10 of 12 surveyed frameworks (AutoGen, LangGraph, CrewAI, MetaGPT, FastChat, OpenAI Evals, STORM, Prometheus, LiteLLM, smolagents) ship single-family defaults. Only **inspect_ai** and **SWE-agent** ship cross-family panels as documented, runnable patterns. *(Codex C1 + C5 + D1 + D2; **high confidence**.)*
- **The heterogeneity advantage is conditional on aggregation method and not robust to compute controls.** Maryanskyy 2026 (arxiv:2603.20324) shows diverse-team win rate flips from 0.810 (judge-based selection) to homogeneous-preferred (MoA synthesis); Zhang 2025 (arxiv:2502.08788) and arxiv:2604.02460 show many multi-agent gains vanish under equal-token-budget controls. *(Kimi-7 + Kimi-10 + GLM-13; **medium-high confidence**.)*

---

## Section 2: Consensus Findings (sources agree)

### ★★★ Homogeneous scaling saturates
All four lenses converge: adding more instances of the same model family hits a hard ceiling well before equivalent investment in family diversity does.
- **GLM-01** (arxiv:2602.03794): "2 diverse agents match or beat 16 homogeneous agents across 7 benchmarks"
- **Kimi-9** (Yang 2026, same paper): information-theoretic proof — effective-channels bound shows homogeneous outputs are correlated, causing early saturation
- **Codex C1**: all 5 major frameworks default to one shared model, but the convergent code pattern means observed production gains are bounded by this same saturation regime
- **Opus §2**: classical ensemble theory (Breiman 1996, Condorcet 1785) predicts this directly — uncorrelated errors are the precondition for variance reduction

### ★★★ Different model families exhibit distinct, stable systematic biases
- **GLM-02** (Nasser 2026): Krippendorff α = 0.042; judge-ID accuracy 77.1%
- **Kimi-8** (same paper): N=3,240 evaluations; GPT-4.1 vs GPT-5.2 separation = 99.6%
- **Kimi-6** (Liang et al. 2024, arxiv:2406.07791): position bias varies systematically by model family across N>150,000 evaluations — **most replicated finding in this literature**
- **Opus §2**: "evaluative fingerprints are stable and family-specific" — directly transferred from Nasser as conventional wisdom

### ★★★ Family-diverse panels outperform same-family panels in LLM-as-judge tasks
- **GLM-03** (Verga et al. 2024, PoLL, arxiv:2404.18796): disjoint model families → 7× cheaper, lower bias, beats single GPT-4 judge
- **Kimi-5** (same paper): "disjoint model families" explicitly named as bias-reduction mechanism
- **Codex D2**: inspect_ai is the only framework that ships this pattern as first-class API (`model_graded_qa(model=[...])` + `multi_scorer(..., "mode")`) — confirming the design pattern is implementable but rare
- **Opus §2**: conventional wisdom rests on independence-of-errors assumption transferred from classical ensembles

### ★★ Heterogeneity-vs-scale question hinges on error correlation, not agent count
- **Opus §3** + **Kimi caveat §3** + **GLM Gap 1**: all three lenses identify that the unmeasured variable is the actual pairwise error correlation between frontier model families on real evaluation tasks
- **Opus §5**: proposes the experiment (pairwise correlation matrix across 4-6 frontier families × 5-10 task categories × 500 items × 6-month repeat for drift)

### ★★ Aggregation method moderates the heterogeneity advantage substantially
- **Kimi-10** (Maryanskyy 2026, arxiv:2603.20324): diverse-team win rate = 0.810 under judge-based selection vs. 0.512 under homogeneous; **inverts** to homogeneous-preferred under MoA synthesis; Glass's Δ = 2.07
- **GLM-08** (arxiv:2601.19921): "robust gains require careful diversity, not naive iteration"
- **Codex C5**: in shipped code, "LLM-as-judge = one judge model" — frameworks have not yet operationalized aggregation-aware panel design

### ★★ Compute confounds plague the heterogeneity-vs-scale comparison
- **Kimi-7** (Zhang 2025, arxiv:2502.08788): MAD frequently fails to beat single-agent CoT under compute control; heterogeneity is the salvage variable
- **GLM-13** (arxiv:2604.02460): single agents match/beat multi-agent under equal token budgets on multi-hop reasoning
- **Opus §3**: the strongest dissent — "scaling agent count with moderate soft heterogeneity may achieve comparable calibration improvement to hard heterogeneity at lower operational cost"

### ★★ The "heterogeneity" construct is defined inconsistently across the field
- **Kimi caveat §1**: of 12 papers reviewed, only 5 explicitly test model-family heterogeneity as IV; most conflate with prompt/persona-level diversity
- **Kimi-12** (Gu et al. 2024 survey, arxiv:2411.15594): explicitly flags this conflation as field-level methodology problem
- **GLM** preface: had to re-tag every source with the heterogeneity-stance the source actually tested (family vs. prompt vs. role vs. value)
- **Codex C3**: shipped code shows the same conflation — STORM and MetaGPT call cost-tiered same-family setups "heterogeneous" in documentation

### ★★ Family-level heterogeneity is rarely default in production code
- **Codex C1 + C5 + Summary Table**: 10 of 12 frameworks ship homogeneous defaults; cross-family panels exist only in inspect_ai and SWE-agent
- **GLM-19** (TDS DeepMind): unstructured multi-agent networks amplify errors up to 17.2× vs. single-agent — practitioner-facing warning
- **Opus §4 Case 3**: small-model "heterogeneous" panels assembled for cost reasons can be both more expensive AND less reliable than one frontier model

---

## Section 3: Disputed Findings (sources disagree)

### Dispute 1: Does cross-family heterogeneity reliably produce uncorrelated errors?
- **Pro:** Kimi-5 (PoLL), Kimi-8 (Nasser fingerprints), GLM-14 (arxiv:2604.00026 — cosine similarity 0.56 heterogeneous vs. 0.85 homogeneous → behavioral differentiation), Opus §2 conventional wisdom
- **Contra:** GLM-10 (arxiv:2507.21168 — "diverse LLMs share failure patterns; positive error correlation limits gains"), Opus §3 dissent (shared pretraining corpora dominate architectural diversity), Opus §4 Case 1 + cite of Jimenez 2024 (SWE-Bench shows high cross-family error correlation on hard code tasks)
- **Why they disagree:** different task categories. Tasks where each family's training data covers the answer space (general QA, math, common reasoning) → low correlation, heterogeneity helps. Tasks at the frontier of all training distributions (formal verification, rare-language code, narrow scientific domains) → high correlation, heterogeneity collapses to homogeneity.
- **What would resolve it:** the Opus §5 proposed experiment — pairwise Pearson correlation matrix on binary correct/incorrect outcomes across frontier families × task categories with ≥500 items each. Not yet published.

### Dispute 2: Does RLHF alignment erase pretraining-lineage diversity?
- **Pro alignment-homogenizes:** GLM-23 (OpenReview "Stop Automating Peer Review Without Rigor", cJhlquXIuS — peer-reviewed venue argues instruction-tuned LLMs produce homogeneous outputs)
- **Contra (alignment preserves divergence):** Kimi-8 / GLM-02 (Nasser 2026 — judges remain 99.6% separable post-alignment; fingerprints are stable), Kimi-6 (Liang et al. — position bias still model-family-specific post-alignment), GLM-14 (architectural heterogeneity produces reproducible behavioral differentiation even with similar prompting)
- **Why they disagree:** different measurement primitives. Nasser/Liang measure systematic *bias profiles*, which alignment may not fully suppress. The OpenReview critique measures *output homogeneity*, which alignment optimization explicitly targets. Both can be true simultaneously: outputs converge on surface form while biases remain divergent.
- **What would resolve it:** measuring effective-family-diversity post-alignment on evaluation tasks specifically — not generation tasks. No paper does this yet.

### Dispute 3: Is the multi-agent advantage real or a compute confound?
- **Pro multi-agent has real advantage:** GLM-04 (X-MAS +46.67pp on AIME-2024), Kimi-9 (Yang 2026 theoretical proof), Kimi-4 (MoA), Kimi-11 (X-MAS large-scale empirical)
- **Contra (compute confound):** GLM-13 (arxiv:2604.02460 — equal token budgets eliminate multi-agent gains on multi-hop reasoning), GLM-06 (CMU 2602.18998 — sequential scaling plateaus at context ceiling), Kimi-7 (Zhang 2025 — MAD ≤ CoT baseline at controlled compute), Kimi caveat §3
- **Why they disagree:** the X-MAS / MoA / Yang results control for *number of model families* (a configuration variable) but not for *total inference FLOPs* (a compute variable). The 2604.02460 / 2602.18998 / Zhang papers control for compute but at the cost of restricting the agent topology. Neither side measures both correctly.
- **What would resolve it:** factorial 2×2 design — {homogeneous, heterogeneous} × {fixed-K agents, fixed-token-budget} on the same task suite. Not yet published.

### Dispute 4: Is OneFlow's concession that heterogeneous MAS remains advantageous strong evidence for heterogeneity necessity?
- **Pro:** GLM-05 (OneFlow, arxiv:2601.12307) — explicitly concedes single-LLM cannot capture truly heterogeneous workflows
- **Contra:** the concession is silence-on-test, not affirmative-evidence; OneFlow did not measure heterogeneous MAS, it folded homogeneous MAS into single-agent execution
- **Why they disagree:** this is more semantic than empirical — a paper conceding it didn't test X is not evidence X holds
- **What would resolve it:** OneFlow-style folding applied to heterogeneous MAS to test whether heterogeneous gains *also* disappear under KV-cache-reuse single-agent simulation

### Dispute 5: Does Rajan 2025 actually exist?
- **Pro:** the research plan cites it as the load-bearing formal-proof anchor
- **Contra:** five independent searches in lens-kimi (Google Scholar, arXiv author search, Semantic Scholar) returned zero matches; lens-glm could not locate it in web sweep; lens-opus flags it as "preprint status unverified"
- **Why they disagree:** either the citation is mislabeled (wrong author, wrong title, different venue) or it does not exist as described. The plan may have hallucinated the reference.
- **What would resolve it:** the consumer of this synthesis providing the actual arxiv ID, DOI, or PDF. **Per Nasser 2026's anti-vote-rule (arxiv:2601.05114 §discussion), we report this dispute honestly rather than treating "3 lenses cannot find it" as proof of non-existence.**

---

## Section 4: Cross-Lens Gaps (what's NOT in any source)

### Gap A: No controlled ablation of family-vs-prompt heterogeneity at equal compute
Identified independently by **GLM Gap 1**, **Kimi caveat §3**, and **Opus §5**. No paper isolates family diversity while controlling both (a) prompt diversity and (b) total inference FLOPs. The single most decisive experiment is unpublished.

### Gap B: Rajan 2025 not surface-locatable
Independently flagged by **GLM-25**, **Kimi anchor section**, **Opus footnote 5**. The plan's central formal-proof citation cannot be verified. The closest available analogue is Yang et al. 2026 (arxiv:2602.03794, information-theoretic effective channels), which proves sufficient-not-necessary saturation of homogeneous scaling.

### Gap C: Post-alignment effective-diversity measurement
**GLM Gap 3** and **Opus §3 dissent**. No paper directly measures whether RLHF alignment compresses the effective diversity of nominally different model families. The closest data point is Nasser 2026's 99.6% post-alignment judge-separability, but that measures bias-profile divergence, not behavioral diversity on evaluation tasks.

### Gap D: Production configuration data
**GLM Gap 4** and **Codex C1 + C5**. We know what frameworks *afford* (cross-family in most), what they *default to* (homogeneous in most), and what their example notebooks *demonstrate* (homogeneous in 10 of 12). We do not know what real teams configure in production. inspect_ai is the only framework where the docs require users to think about this.

### Gap E: Narrow-domain heterogeneity research
**GLM Gap 5** + **Opus §4 Case 1**. Most studies use math (AIME), general QA, or open-domain evaluation. Specialized domains (legal, medical, peer review, formal verification) have little controlled heterogeneity research. ICLR-2025 large-scale study (GLM-15, arxiv:2504.09737) is an exception but does not isolate heterogeneity as an independent variable.

### Gap F: Replication studies for 2026 preprints
**GLM Gap 6** + **Kimi caveat §2**. arxiv:2602.03794, arxiv:2601.05114, arxiv:2603.20324, arxiv:2506.18348, arxiv:2604.00026 are all 2026 preprints with no independent replication yet. The strongest theoretical (Yang) and mechanistic (Nasser) anchors of the consensus position are single-source claims.

### Gap G: Non-English / Chinese AI lab research
**GLM Gap 7**. The Qwen, Baichuan, DeepSeek, Yi, Moonshot model families create natural diversity opportunities that western-arxiv-indexed work under-covers. Non-English research on heterogeneous agent panels is absent from this synthesis.

### Gap H: Pairwise error correlation matrix on frontier evaluation tasks
**Opus §5 central question** + **Codex outlier signal**. No paper publishes the actual cross-family pairwise error correlation structure on real evaluation workloads, broken down by task category. Without this data, neither side of the dispute can be settled definitively.

### Gap I: Aggregation-method × heterogeneity interaction at scale
**Kimi-10 (Maryanskyy)** is the only paper measuring this interaction, with N=210 internally replicated (Spearman ρ = 0.90). External replication at larger N has not been published.

### Gap J: Tool-augmented heterogeneity vs. LLM-only heterogeneity
**Opus Recommendation 5**. The trade-off between adding another LLM judge vs. adding a non-LLM verifier (static analyzer, formal checker, test runner) is unmeasured in this literature. Practitioner intuition (per Opus) is that for narrow technical domains, tool augmentation strictly dominates additional LLM diversity, but this is not empirically tested.

---

## Section 5: Numbered Recommendations

### 1. Before deploying a heterogeneous panel, measure inter-family error correlation on your specific task category.
- **Supporting lens(es):** Opus §6 Recommendation 1; Kimi caveat §4 (task-distribution bias)
- **Condition under which it would be wrong:** Your task distribution closely matches a published benchmark (AIME, MT-Bench, FairEval) where heterogeneity benefits are independently documented — in which case you can transfer the benchmark's correlation estimates rather than running your own ablation.

### 2. Treat "heterogeneity" as a spectrum from prompt → persona → temperature → family → architecture, and report which level you are testing.
- **Supporting lens(es):** Kimi caveat §1 (definition-conflation problem); GLM source-tagging methodology; Opus §1 "soft vs. hard heterogeneity"
- **Condition under which it would be wrong:** Your evaluation pipeline has only one knob (e.g., LangChain default) — in which case scoring at the *configurable* heterogeneity level is the only meaningful test.

### 3. If using inspect_ai or building a panel from scratch, default to ≥3 distinct model families with majority-vote aggregation; do not stack instances of one family.
- **Supporting lens(es):** Codex D2 (inspect_ai's `multi_scorer(..., "mode")` is shipped first-class for this); GLM-03 / Kimi-5 (PoLL); GLM-04 / Kimi-11 (X-MAS monotonic improvement)
- **Condition under which it would be wrong:** Your task is in a narrow technical domain (Lean4 proofs, Rust unsafe semantics, rare language code review) where all frontier families share blind spots — see Opus §4 Case 1.

### 4. Treat aggregation method as a first-class design variable, not a nuisance parameter.
- **Supporting lens(es):** Kimi-10 (Maryanskyy 2026, Glass's Δ = 2.07 for judge-vs-synthesis); GLM-08 (arxiv:2601.19921 design recommendations); Codex C5 (frameworks ship one-judge defaults — this is the gap)
- **Condition under which it would be wrong:** Your task admits a single dominant aggregation strategy by structure (e.g., binary classification with rare positive class → majority-vote is uniquely well-justified).

### 5. Do not treat Rajan 2025 as a confirmed citation until independently verified.
- **Supporting lens(es):** Kimi anchor section (5 failed searches); GLM-25 (lookup required); Opus footnote 5 (preprint status unverified)
- **Condition under which it would be wrong:** The plan author provides a DOI, arxiv ID, or PDF showing the citation is real. **Per Nasser 2026's evidence on vote-rule bias amplification (arxiv:2601.05114 §discussion), the fact that 3 of 4 lenses cannot locate it is reported as honest dispute, not vote-rule decision.**

### 6. Treat Nasser 2026 as suggestive single-source evidence, not confirmed mechanistic theory, until independent replication.
- **Supporting lens(es):** Kimi-8 (single-author preprint, arXiv-only, N=3,240 from one source); GLM-02 (high confidence on internal validity but no replication); Opus footnote 6
- **Condition under which it would be wrong:** Independent replication is published. The author's release of github.com/wajid-nasser/evaluative-fingerprints makes this falsifiable in principle — any team can attempt replication today.

### 7. Run a compute-controlled head-to-head before claiming heterogeneity beats scale.
- **Supporting lens(es):** Kimi-7 (Zhang 2025); GLM-13 (arxiv:2604.02460); Kimi caveat §3 (compute non-control as dominant methodological problem)
- **Condition under which it would be wrong:** Your task is so expensive per inference that compute equivalence is dominated by other factors (latency, multi-vendor reliability, governance) — in which case practical operational concerns outweigh the compute-control comparison.

### 8. For narrow technical domains, prefer tool augmentation over additional LLM judges.
- **Supporting lens(es):** Opus §6 Recommendation 5; Opus §4 Case 1 (SWE-Bench correlation findings, Jimenez 2024); Codex Prometheus example as anti-pattern (single specialist model, no panel)
- **Condition under which it would be wrong:** No tools are available (purely subjective tasks: style, tone, coherence) or the tool itself has lower precision than an LLM judge on the relevant subdomain.

### 9. Monitor evaluative-fingerprint drift across model version updates.
- **Supporting lens(es):** Kimi-8 / GLM-02 (Nasser fingerprints stable within-version); Opus §4 Case 2 (calibration drift under distribution shift)
- **Condition under which it would be wrong:** You use frozen open-weight checkpoints — drift is absent by definition.

### 10. Adopt inspect_ai's API surface or equivalent (`model_graded_qa(model=[list])` + named `model_roles`) rather than rolling custom multi-model judging.
- **Supporting lens(es):** Codex D2 (inspect_ai is sole framework shipping this as first-class API); Codex stack-rank (inspect_ai = top of maintainability ranking, UK AISI active maintenance)
- **Condition under which it would be wrong:** Your existing stack has deep investment in another framework (LangGraph, AutoGen, CrewAI) — in which case adding `litellm`-routed cross-family panel logic on top of your existing orchestration is lower switching cost than migrating to inspect_ai.

---

## Section 6: Source Manifest

### From lens-glm.md (web sweep, BREADTH)
- arxiv:2602.03794 — "Understanding Agent Scaling in LLM-Based Multi-Agent Systems via Diversity" (Yang et al. 2026)
- arxiv:2601.05114 — Nasser 2026 "Evaluative Fingerprints" + github.com/wajid-nasser/evaluative-fingerprints
- arxiv:2404.18796 — Verga et al. 2024 "Replacing Judges with Juries" (PoLL)
- arxiv:2505.16997 — Li et al. 2025 "X-MAS"
- arxiv:2601.12307 — "Rethinking the Value of Multi-Agent Workflow" (OneFlow)
- arxiv:2602.18998 — CMU "Benchmark Test-Time Scaling of General LLM Agents"
- arxiv:2508.17536 — Choi et al. NeurIPS 2025 "Debate or Vote" + github.com/deeplearning-wisc/debate-or-vote
- arxiv:2601.19921 — "Demystifying Multi-Agent Debate"
- link.springer.com/article/10.1007/s44443-025-00353-3 — A-HMAD (Al-Khatib et al. 2025)
- arxiv:2507.21168 — "Diverse LLMs or Diverse Question Interpretations?"
- arxiv:2511.15714 — "Majority Rules: LLM Ensemble"
- arxiv:2512.01786 — "Who Judges the Judge? LLM Jury-on-Demand"
- arxiv:2604.02460 — "Single-Agent LLMs Outperform Multi-Agent Systems on Multi-Hop Reasoning"
- arxiv:2604.00026 — "Behavioral Differentiation Without Role Assignment"
- arxiv:2504.09737 — Stanford "Can LLM Feedback Enhance Review Quality? ICLR 2025 RCT"
- arxiv:2512.10665 — "Dynamics of Multi-Agent LLM Communities Driven by Value Diversity"
- arxiv:2601.08003 — "LLM Review: Blind Peer Review Feedback"
- arxiv:2506.18348 — IDVSCI "Dynamic Knowledge Exchange and Dual-diversity Review"
- towardsdatascience.com/the-multi-agent-trap/ — TDS reporting Google DeepMind research
- getmaxim.ai/blog/llm-as-a-jury/ — Maxim AI PoLL practitioner blog
- eugeneyan.com/writing/llm-evaluators/ — Eugene Yan LLM-as-judge essay
- medium.com/@mjgmario/single-agent-vs-multi-agent-systems-when-coordination-helps-hurts-and-pays-off-57735ee7916d
- openreview.net/pdf?id=cJhlquXIuS — "Stop Automating Peer Review Without Rigor"
- arxiv:2510.13143 — "Stable LLM Ensemble: Interaction between Example Representativeness and Diversity"
- [lookup] Rajan 2025 — UNVERIFIED, not located by web sweep
- effloow.com/articles/agent-test-time-compute-scaling-context-ceiling-2026
- beancount.io/bean-labs/research-logs/2026/05/31/single-agent-outperforms-multi-agent-equal-token-budget

### From lens-kimi.md (academic deep-dive, RIGOR)
- arxiv:2305.14325 — Du et al. 2023 (NeurIPS 2023, peer reviewed)
- arxiv:2308.07201 — Chan et al. ChatEval (ICLR 2024, peer reviewed)
- arxiv:2306.05685 — Zheng et al. MT-Bench (NeurIPS 2023, peer reviewed)
- arxiv:2406.04692 — Wang et al. Mixture-of-Agents (ICLR 2025, peer reviewed)
- arxiv:2404.18796 — Verga et al. PoLL (preprint)
- arxiv:2406.07791 — Liang et al. "Judging the Judges" position bias (preprint v9)
- arxiv:2502.08788 — Zhang et al. 2025 "Stop Overvaluing Multi-Agent Debate" (preprint)
- arxiv:2601.05114 — Nasser 2026 (preprint, single-author, no peer review)
- arxiv:2602.03794 — Yang et al. 2026 (preprint, no peer review)
- arxiv:2603.20324 — Maryanskyy 2026 "When Agents Disagree" (preprint, single-author)
- arxiv:2505.16997 — X-MAS (preprint)
- arxiv:2411.15594 — Gu et al. 2024 survey (preprint v6)
- arxiv:2410.02736 — Ye et al. "Justice or Prejudice" (cited via Nasser citation chain)
- arxiv:2401.00595 — Mizrahi et al. "State of What Art?" (cited via Opus)
- [lookup] Rajan 2025 — UNVERIFIED after 5 searches

### From lens-codex.md (shipped code, PRACTICE)
- github.com/microsoft/autogen — `python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py:80`, `_selector_group_chat.py:90`, `_magentic_one_group_chat.py:77`
- github.com/langchain-ai/langgraph — `docs/docs/tutorials/multi_agent/multi-agent-collaboration.ipynb:cell-9`, `hierarchical_agent_teams.ipynb:cell-11`
- github.com/crewAIInc/crewAI — `lib/crewai/src/crewai/constants.py:348`, `utilities/llm_utils.py:82-86`, `agent/core.py:215-219`, `crew.py:248-264`
- github.com/openai/evals — `evals/elsuite/modelgraded/classify.py:18-37`, `cli/oaieval.py:183`, `classify_utils.py:1-10`
- github.com/UKGovernmentBEIS/inspect_ai — `src/inspect_ai/scorer/_model.py:33-37, 148-155`, `_multi.py:14-35`, `_eval/eval.py:97`, `model/_model.py:1583-1620`
- github.com/SWE-agent/SWE-agent — `config/benchmarks/250212_sweagent_heavy_sbl.yaml:11-12, 182-183`, `sweagent/agent/reviewer.py:129, 558, 565-568`, `config/benchmarks/250225_anthropic_filemap_simple_review.yaml:68-69`
- github.com/stanford-oval/storm — `examples/storm_examples/run_storm_wiki_gpt.py:55-80`, `knowledge_storm/storm_wiki/modules/knowledge_curation.py:28-45`, `knowledge_storm/lm.py:47-65`
- github.com/geekan/MetaGPT — `metagpt/configs/llm_config.py:60, 64`, `context_mixin.py:32-45`, `config/config2.example.yaml:22-48`, `metagpt/roles/role.py:161-173`
- github.com/huggingface/smolagents — `examples/multi_llm_agent.py:8-30, 33-36, 37`
- github.com/lm-sys/FastChat — `fastchat/llm_judge/gen_judgment.py:49, 137-162, 183`
- github.com/BerriAI/litellm — `cookbook/Evaluating_LLMs.ipynb:cell-1, cell-2`, `litellm/router.py:235, 363`
- github.com/prometheus-eval/prometheus-eval — `libs/prometheus-eval/prometheus_eval/judge.py:18-42, 30-40`

### From lens-opus.md (narrative analysis, THEORY)
- Breiman, L. (1996). *Bagging Predictors.* Machine Learning 24(2)
- Condorcet (1785). *Essai sur l'application de l'analyse à la probabilité des décisions...*
- arxiv:2306.05685 — Zheng et al. 2023 MT-Bench (also in Kimi/GLM)
- arxiv:2310.06770 — Jimenez et al. 2024 SWE-bench
- arxiv:2401.00595 — Mizrahi et al. 2024 "State of What Art?"
- Schapire (1990) boosting; Dietterich (2000) ensemble methods (foundational, no URL)
- [unverified] Rajan et al. 2025 — flagged for lens-kimi verification
- [unverified-then-verified] Nasser et al. 2026 — confirmed by Kimi as arxiv:2601.05114

### Cross-lens cited but not in primary source list
- arxiv:2502.08788 (Zhang 2025) — appears in Kimi and Opus dissent
- github.com/wajid-nasser/evaluative-fingerprints (Nasser code release)
- github.com/deeplearning-wisc/debate-or-vote (Choi NeurIPS 2025 code)

---

*Synthesis composed from 4 parallel lens reports per the research-synthesis recipe. Per Nasser 2026 (arxiv:2601.05114), disputed findings are reported honestly without vote-rule aggregation. Consensus markers ★/★★/★★★ indicate 2/3/4-lens convergence respectively. Rajan 2025 citation remains unverified and is flagged at every load-bearing reference.*
