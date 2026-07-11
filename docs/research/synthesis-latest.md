# Synthesis — Is mini-ork actually intelligent?

**Run:** `run-1783718682-45995`
**Question:** Which decision + learning mechanisms are INTELLIGENT (close a read-write loop from past runs) versus NOT-INTELLIGENT (static config, write-only tables, dead code)? Direct answer to the COMPOSER / interleaved-runs feature-isolation question.
**Date:** 2026-07-10
**Scope:** `bin/mini-ork*`, `lib/*.sh`, `mini_ork/**`, `recipes/**`, `config/**` at HEAD of `feat/gepa-gradient-fixes`.

Lens sources (read in full before composing):
- `lens-glm.md` — breadth surface-scan (54 static sites, 11 DB tables tagged LIVE/WRITE-ONLY/DEAD)
- `lens-kimi.md` — methodological rigor (6 closed-loop criteria, per-loop verdict table, 18 arxiv citations)
- `lens-codex.md` — code-pattern survey (11 public frameworks, comparative table)
- `lens-opus.md` — deep narrative (composer-question end-to-end trace)

---

## 1. TL;DR

- **★ Three genuine INTELLIGENT loops**, but only one is load-bearing for routing. GRPO lane routing (write→read symmetry, credit-assigned, decay-aware), gradient→context/role-evolver feedback, and within-run GEPA Pareto search. *(GLM-7 + Kimi-1+2+5; high confidence.)*
- **★★ Three structurally correct mechanisms collapse toward STATIC in practice** because of reward starvation and a 3-sample floor. The lane router clears 5 of Kimi's 6 criteria on paper but degrades to noise-dominated at the floor; the explore rule never converges (constant ε-greedy); the reward rail silently NULLs on most runs. *(GLM-9 + Kimi-3+4.)*
- **★★ At least five DB tables look like memory but aren't consulted**: `emergent_patterns`, `prompt_win_rates`, `conductor_decisions.outcome/realized_score`, `run_artifact_edges`, plus the WRITE-ONLY reflection chain (`gradient_records`/`pattern_records` w.r.t. routing). *(GLM-12 + Kimi-6.)*
- **★★ Composer / interleaved-runs verdict: MIXED-WITH-LIMITS leaning GLOBAL-POLLUTED under default env.** `objective_domain="code-delivery"` is the universal default; `code_region` collapses feature dirs to top-level directory; `context_prior_runs_md`, `topology_win_rates`, `prompt_win_rates` all pool on `task_class` alone. Opt-in feature isolation is theoretically possible via `MINI_ORK_OBJECTIVE_DOMAIN=<feature>` but undocumented. *(Kimi-7 + Opus-3+4; high confidence.)*
- **★ No public framework closes the full outcome→policy loop** — every one of the 11 surveyed (LangGraph, AutoGen, OpenHands, SWE-agent, MetaGPT, AutoGPT, Letta, Mem0, LlamaIndex, Agno, CAMEL) persists state or memory but none automatically turns graded outcomes into changed routing/prompt/topology. Mini-ork is not behind the field here, but it is also not ahead — closing this gap would be a genuine contribution. *(Codex-5 + Kimi-1.)*

---

## 2. Consensus findings (sources agree)

Items where ≥2 lenses converge. ★ = 2-lens, ★★ = 3-lens, ★★★ = 4-lens.

### ★★ GRPO lane routing is the only adaptive decision-maker (GLM-1 + Kimi-1)

`agent_performance_memory` ← writes from `lane_router.py:235-245` (EMA-blended `relative_advantage` with shrinkage, recency, defect penalty); `lane_*_advantage` ← writes from `lane_router.py:247-279`; `preferred_lane` ← reads at `lane_router.py:286-330`; `decide()` consumes at `mini_ork/ported/decision_service.py:284-287` and `lib/decision_service.sh:80-217`. Region→domain→global fallback (GLM-21, Kimi-1) gives graceful degradation. This is the closest thing to a closed loop in the repo. The Kimi verdict "INTELLIGENT but NOISE-DOMINATED at floor" is the more honest reading; GLM calls it "INTELLIGENT closed loop" without quantifying the floor's variance cost. Both agree: it is the only one.

### ★★ Reward starvation is the structural failure mode of the learning rail (GLM-12 + Kimi-3)

`compute_reward_g` (`mini_ork/trace_store.py:28-40`) writes NULL when anchor==0; `grade_run_reward` (`mini_ork/trace_store.py:195-223`) only fires when `rubric.json` exists. The lane router skips NULL rows (`lib/lane_router.sh:173-187`). GLM identifies the 3-level step function at `bin/mini-ork-execute:289-299` (`_mo_reward_from_status` maps to 1.0/0.0/0.5) as the weak link; Kimi calls out the rubric gate as the reason most runs never produce `reward_g`. Both converge: the rail is mathematically sound but practically starved.

### ★★ Constant ε-greedy exploration cannot converge (GLM-8 + Kimi-4)

`MO_LEARNING_EPSILON=0.10` default at `lib/decision_service.sh:121`, no decay schedule. Kimi cites Auer et al. 2002 and Langford & Zhang 2008 — constant ε ⇒ linear cumulative regret. GLM notes "10% of dispatches ignore the learned route forever, no anneal" as risk shape. Worse, Kimi surfaces that exploration is gated `if learned_route:` (decision_service.py:289-297) — cold slices never explore at all, so the system cannot bootstrap its first sample.

### ★★ The reflection chain (`gradient_records`/`pattern_records`/`emergent_patterns`) is memory-as-staging, not memory-as-decision-input (GLM-12 + Kimi-5)

GLM tags `gradient_records` LIVE for context_assembler (failure-mode injection) and role_evolver (proposals); `pattern_records` LIVE as input to `emergent_patterns` promotion; `emergent_patterns` WRITE-ONLY (no consumer in routing/decision/context paths). Kimi calls the chain "WRITE-ONLY w.r.t. routing" — intelligent as memory substrate, not as a closed loop. Both agree: this is the largest gap between "the system looks reflective" and "reflection changes routing."

### ★★ Composer / interleaved-runs feature isolation is nominally supported but in practice global-polluted (Kimi-7 + Opus-3+4)

Kimi's formal analysis: `lane_router`'s region and domain tiers DO carry `objective_domain` end-to-end, so feature isolation is *theoretically* achievable. But (a) `objective_domain` defaults to `"code-delivery"` (`bin/mini-ork-execute:352`); (b) `code_region` collapses feature dirs to top-level directory (`bin/mini-ork-execute:1665-1737`, `_mo_infer_trace_code_region`); (c) `context_prior_runs_md` (`lib/context_assembler.sh:457-469`), `topology_preferred` (`lib/topology.sh:127-136`), `rho_top_prompts` (`lib/rho_aggregator.sh:108-111`) all pool on `task_class` alone. Opus's narrative trace says "MIXED-WITH-LIMITS leaning GLOBAL-POLLUTED under default env." Both lenses reach the same verdict via different paths.

### ★★ Every cross-run learning surface except lane routing is keyed on `task_class` alone (Opus-3 + Kimi-7)

`context_prior_runs_md`, `topology_preferred`, `rho_top_prompts`, GRPO grouping at `bin/mini-ork-execute:651` (`(node_type, task_class)`), agent_performance_memory global fallback (no `objective_domain` predicate, `lane_router.py:319-327`). The shared assumption is "task_class is the transfer boundary" — Kimi reads this from the SQL predicates; Opus reads it from the architectural commentary in `docs/architecture/techniques-compendium.md:121`. Both agree this is the load-bearing assumption and both agree it is contested in the literature.

### ★ Static values dominate the decision surface (GLM-1..54 + Kimi-7+8)

54 entries in GLM Catalog A; Kimi singles out reward starvation and constant ε as structural. The convergence: every per-run decision input that is *not* derived from prior traces is hardcoded somewhere (model names, fallback chains, reward weights, sample floors, budget caps, classifier keywords). GLM identifies a critical dual-edit hazard: `lib/process_reward.sh:99` and `mini_ork/learning/process_reward.py:55` mirror the same weights; `lib/lane_router.sh:87-90` and `mini_ork/lane_router.py:35-38` mirror the same hyperparameters.

### ★ No public framework closes the full outcome→policy loop (Codex-5 + Kimi-1)

Codex surveyed 11 frameworks; zero deliver "outcome/eval → credit assignment → policy/prompt/topology selection → future run." The dominant public pattern is **CLOSED context-state** (memory injection into next prompt) or **PERSISTENCE-ONLY** (durable state for human inspection). Kimi cites Shen et al. 2026 [arxiv:2606.16733] as the formal grounding for why write-read symmetry is the persistence half of any RL guarantee. Convergence: the field has not solved this problem either; mini-ork's GRPO loop is *one* of the few actually-attempted closures.

### ★ Within-run intelligence ≠ cross-run intelligence (Kimi-5 + Codex-6)

Kimi: GEPA's Pareto front is intelligent within one optimize run but rebuilt each run (gepa.py:62, no cross-run archive). Codex: "Within-run closure is kept separate from cross-run reuse. LangGraph closes resume/replay; SWE-agent closes reviewer/retry; AutoGPT records node execution. Those loops improve reliability without implying that run N learns a policy from runs 1..N-1." This is the most important conceptual distinction in the audit.

### ★★ The reward signal is too coarse to discriminate at feature granularity (Opus-4-C + GLM-12)

PRM weights at `lib/process_reward.sh:1-20` and `mini_ork/learning/process_reward.py:55-61` are a hardcoded 0.40/0.20/0.10/0.15/0.10/0.05 linear blend over trace observables. Opus Case C: a composer run that shipped a regression is indistinguishable from a clean run as long as `status=success`. GLM: the 3-level `_mo_reward_from_status` map collapses nuanced verdicts. Skalse et al. 2022 [arxiv:2209.13085] (reward hacking) is Opus's theoretical anchor. Both agree: the reward rail cannot form the per-feature GRPO signal that would make isolation valuable.

### ★ Public frameworks mandate composite scope; mini-ork makes scope best-effort (Codex-2 + Opus-6)

Codex convergent pattern #1: "Identity-scoped state is the unit of continuity" (LangGraph thread_id, OpenHands conversation_id, Letta agent/block, Mem0 user/agent/run, Agno user, AutoGPT execution/parent). Opus recommendation #6: "make a bare `mini-ork run` inherit its parent run's `objective_domain` (via `MINI_ORK_PARENT_RUN_ID`) when the caller doesn't stamp one." Both lenses converge: scope should be a mandatory composite namespace, not a default-everything fallback.

---

## 3. Disputed findings (sources disagree)

### Dispute 1 — Is the lane router "INTELLIGENT" or "EFFECTIVELY-STATIC"?

**GLM position:** Tags the lane router as a **genuinely closed learning loop** in Catalog B "What is genuinely INTELLIGENT" (#1). The write→read symmetry is clean; region/domain/global fallback is in place; the only qualifier is the static reward rail feeding it.

**Kimi position:** "INTELLIGENT but **NOISE-DOMINATED at floor**." The loop formally closes but the 3-sample floor combined with NULL-reward starvation makes the learned preference indistinguishable from sampling noise (He & Gu 2025 [arxiv:2503.12020]; Tang et al. 2025 [arxiv:2502.10985]). Effective verdict: the loop "degrades toward static for three compounding reasons" — reward starvation, noise at the floor, cold-start blindness.

**Why they disagree:** GLM verified write→read symmetry by grep; Kimi applied a statistical-validity test to the same evidence. GLM's framing is structural ("does the loop close?"); Kimi's framing is operational ("does the loop's output drive a better-than-random decision?"). Both are correct on their own terms.

**My judgment:** Kimi is more useful for a practitioner. The structural closure is necessary but not sufficient — a loop that closes but emits noise is not a learner in any operational sense. The correct reclassification is **INTELLIGENT (structure) / STATIC (de-facto outcome)**.

**Additional evidence needed:** η² (variance explained) of the learned advantage after conditioning on `task_class`/`objective_domain`/`code_region`. Opus §5 sketches the experiment design; it can be run on existing `state.db` data without code changes.

### Dispute 2 — Is `objective_domain` a feature isolation lever or a consumer bucket?

**Opus position (from migration `0042` + techniques-compendium.md:121):** The official framing is consumer identity — "code-delivery" vs "book-gen." Within "code-delivery," runs of different features pool. The renaming recommendation #1 ("rename the env var to reflect its dual role") is based on this reading.

**Codex position (convergent pattern #1):** Identity-scoped state should be a *mandatory composite namespace* such as `(objective_domain, workflow_version_id, feature_id)`, not a single bucket. This implies `objective_domain` SHOULD serve as feature isolation if the user opts in.

**Why they disagree:** Opus is reading what mini-ork actually does (single bucket, consumer framing); Codex is prescribing what mini-ork should do (composite scope). Both are correct in their frame.

**My judgment:** The current implementation is the consumer-bucket design (Opus is right about the code). The codebase pattern that *would* make it a feature-isolation lever already exists in `lane_*_advantage` (Kimi-1+7) — the env var just isn't documented as such. The reconciliation: `objective_domain` is *technically* feature-isolated at the lane-router region/domain tiers, but the docs and the global fallback keep it consumer-bucketed in practice.

**Additional evidence needed:** A scan of how many prior runs actually set `MINI_ORK_OBJECTIVE_DOMAIN` to a non-default value vs left it at "code-delivery." If <10%, the lever is dormant regardless of capability.

### Dispute 3 — Are the WRITE-ONLY tables the right next thing to wire, or are they correctly dead?

**GLM position (Catalog B):** `emergent_patterns`, `prompt_win_rates`, `conductor_decisions.outcome` are WRITE-ONLY — "the classic 'looks like memory, isn't consulted' case." Highest-leverage fix #1: read `conductor_decisions.realized_score` back into the conductor's predicted-score model.

**Kimi position (criterion 1):** Write-only reflection rows are WRITE-ONLY but if wired back into routing without a judge gate, they risk "memory confabulation" (Dixit et al. 2026 [arxiv:2605.29463]). The recommendation is extract→distill→verify before any reflection row becomes a routing input.

**Why they disagree:** GLM is identifying a closed-loop gap (read is missing). Kimi is identifying a correctness gap (if you just close it naively, you risk poisoning the rail with confident-but-wrong self-diagnoses).

**My judgment:** Both are right. The fix order should be (1) build the extract→distill→verify pipeline (Kimi's safety argument), then (2) wire the verified rows into routing/context (GLM's closure argument). Naively closing the gap first is exactly the kind of self-poisoning failure mode that makes the audit worth doing.

**Additional evidence needed:** A controlled test where promoted patterns are surfaced to one run group and withheld from another; compare downstream outcome variance to estimate whether the wiring would help or hurt.

---

## 4. Cross-lens gaps (what's NOT in any source)

Items the lenses collectively did NOT investigate or answer. Candidates for future research:

1. **η² for feature after task_class is controlled** (Opus §5 sketched this; nobody ran it). Without it, the entire feature-isolation debate is unresolvable in principle.
2. **Realized-score regression.** GLM identified the missing edge; nobody has measured how much variance in conductor accuracy would be explained by `realized_score` regressed onto the predicted-score model's `0.3` gain.
3. **Eval-node rollout.** The eval-after-verify node from `internal-docs/research/2026-07-03-adding-eval-to-miniork-run-flow.md` would solve the reward-starvation problem by guaranteeing a graded `reward_g` on every run. Nobody estimated the eval-cost-per-run vs the variance-reduction payoff.
4. **Empirical shrinkage.** Adaptive sample-floor (Opus recommendation #5) with empirical-Bayes shrinkage. Theoretical benefit is clear; nobody benchmarked the cost in mini-ork's specific reward distribution.
5. **Cross-framework GRPO audit.** Codex surveyed persistence/memory but did not check whether any framework has a similar GRPO-on-prior-outcomes loop. The closest is SWE-agent's within-run best-of-attempt — it is not cross-run.
6. **Performance under non-stationary task mix.** Kimi cited Tang 2025 on Elo/BT misspecification but did not measure how mini-ork's diff-in-means advantage degrades when the lane population shifts (e.g., adding a new lane to the family).
7. **`MINI_ORK_PARENT_RUN_ID` lineage propagation.** Opus recommendation #6 notes the write sites but no lens tested whether parent→child inheritance actually works end-to-end in the recursive spawn path.

---

## 5. Numbered recommendations

What a thoughtful practitioner should DO with this synthesis today. Each item: action, supporting lenses, condition under which it would be wrong.

1. **Promote `emergent_patterns` (status='proposed'→'approved') into `context_assembler`'s failure-mode injection path.**
   - Lenses: GLM-12 (highest-leverage #2), Kimi-3 (criterion 1 fails today)
   - Wrong if: pattern promotion has no judge-gate; the patterns could be confidently-wrong self-diagnoses (Kimi §6 caveat; Dixit 2026 [arxiv:2605.29463]).
   - Pre-condition: build extract→distill→verify pipeline first, then wire.

2. **Read `conductor_decisions.realized_score` back into the conductor's predicted-score model** (replace fixed `0.3` gain with fitted/EMA coefficient).
   - Lenses: GLM-12 (highest-leverage #1), Opus §4 case A (interleaved-runs pollution requires the conductor to self-correct)
   - Wrong if: the predicted-score model is dominated by topology/lane-advantage terms that overwhelm the realized-score signal — measure first.
   - Source: write at `bin/mini-ork-conductor:248-256`; current reader is only `scripts/smoke-learning-loops.sh:142`.

3. **Run the η² experiment from Opus §5 on existing `state.db` data** before investing in feature-scoping infrastructure. SQL-only, no code changes.
   - Lenses: Opus §5 (full design), Kimi-7 (the formal reading), GLM-7 (region advantage write site)
   - Wrong if: the experiment is run on rows that pre-date the `objective_domain` column (migration `0042`); restrict to post-migration rows.
   - Pre-condition: filter to post-2026 rows where `objective_domain IS NOT NULL`.

4. **Replace `_mo_reward_from_status` (3-level step function at `bin/mini-ork-execute:289-299`) with a graded signal** OR guarantee the eval node populates `reward_g` on every run.
   - Lenses: GLM-12 (Catalog A #12), Kimi-3 (reward starvation), Opus Case C (reward-source coarseness)
   - Wrong if: the eval node is too expensive per-trace or its reward is noisier than the PRM; cost-benefit must be measured first.
   - Source: Skalse et al. 2022 [arxiv:2209.13085] on reward hacking (Opus-4-C).

5. **Document `MINI_ORK_OBJECTIVE_DOMAIN` as a feature-scoping lever** and make the default-on recursive path inherit parent_run_id → objective_domain automatically.
   - Lenses: Opus §6 recommendation #1+#6, Kimi-7 (the lever exists, just dormant), Codex convergent pattern #1 (composite scope)
   - Wrong if: η² experiment (recommendation #3) shows feature explains <5% of reward variance — then the lever is structurally useless.
   - Source: write sites at `bin/mini-ork-spawn:113,123`, `recipes/doc-to-features-loop/lib/per_feature_dispatcher.py:422`; default at `bin/mini-ork-execute:352` and `mini_ork/trace_store.py:125`.

6. **Reconcile the dual-source-of-truth hazards** — three `$50/day` literals (`config/agents.yaml:57`, `bin/mini-ork-conductor:48`, `lib/llm-dispatch.sh:1377`) and two PRM weight tables (`lib/process_reward.sh:99`, `mini_ork/learning/process_reward.py:55`).
   - Lenses: GLM-1 + GLM-2 + GLM-27 + GLM-39 + GLM-43 (dual-edit hazards)
   - Wrong if: the literals are intentionally different (e.g., per-pipeline budget vs global); if so, add a comment explaining.
   - Pre-condition: verify the values are meant to match before consolidating.

7. **Add adaptive sample floor** (empirical-Bayes shrinkage for thin slices) rather than moving the static floor.
   - Lenses: Kimi-3 (criterion 2 marginal), Opus §6 recommendation #5
   - Wrong if: shrinkage adds complexity but the underlying signal is dominated by static heuristics anyway (η² small per recommendation #3).
   - Source: Efron & Morris 1975 (shrinkage theory).

---

## 6. Direct answer to the COMPOSER / interleaved-runs question

**Verdict: MIXED-WITH-LIMITS — leaning GLOBAL-POLLUTED under default env.**

Three regimes, in order of likelihood for real usage:

- **Default (`MINI_ORK_OBJECTIVE_DOMAIN` unset):** **GLOBAL-POLLUTED.** All runs stamp `objective_domain="code-delivery"` (`bin/mini-ork-execute:352`, `mini_ork/trace_store.py:125`). `lane_domain_advantage`/`lane_region_advantage` pool across every eng-team run; `context_prior_runs_md` filters on `task_class` alone; `topology_preferred` and `rho_top_prompts` likewise. The only partition is `code_region` = top-level directory of `files_written[0]` (`bin/mini-ork-execute:1665-1737`), which collapses `src/composer` and `src/auth-middleware` to a shared key. Maximum effective continuity = the sample floor (3 runs) IF the composer's exact `(task_class, node_type)` triple clears it; otherwise global fallback.

- **With `MINI_ORK_OBJECTIVE_DOMAIN=composer` explicitly exported:** **MIXED-WITH-LIMITS.** Region and domain slices (`lib/lane_router.sh:511-545`) DO carry composer identity into `lane_domain_advantage` / `lane_region_advantage`. But `context_prior_runs_md` (planner context), `topology_preferred` (recipe selection), and `rho_top_prompts` (prompt selection) still ignore the domain — those stages remain feature-blind. Effective feature memory reaches ~3 runs of continuity on lane routing alone, with cold-start on every fresh `(task_class, node_type)` pair.

- **With the audit recommendations implemented** (rec #1, #3, #5): Theoretically **FEATURE-ISOLATED** at the lane-routing layer and the planner-context layer, with adaptive shrinkage smoothing cold starts. Empirically untested.

**Maximum effective continuity for one feature over N runs:** 3 runs under default (sample floor); 3 runs with explicit domain stamping (because non-lane stages remain polluted); N runs if the audit's recommendations are implemented and η² confirms the signal exists.

**Confidence:** High on the structural verdict (all 4 lenses converge on the read-site analysis). Medium on the operational "what users actually experience" because no lens measured real-world distribution of `MINI_ORK_OBJECTIVE_DOMAIN` values across prior runs.

---

## 7. Source manifest (for the verifier)

### From lens-glm (`lens-glm.md`)
- File anchors: `mini_ork/lane_router.py:235-330` (write/read), `mini_ork/ported/decision_service.py:98-309` (decide), `mini_ork/trace_store.py:28-223` (reward), `lib/lane_router.sh:8-18,87-525` (bash mirror), `lib/decision_service.sh:80-460` (consume), `lib/process_reward.sh:99-100` (PRM bash), `mini_ork/learning/process_reward.py:55-124` (PRM python), `mini_ork/context_assembler.py:118,249` (gradient read), `lib/gradient_extractor.sh:57,97,383` (idempotent), `lib/reflection_pipeline.sh:266,396-437,494-506` (reflection chain), `bin/mini-ork-conductor:9,48,113,139,154,181,213,207-222,248-256` (header claim vs actual; predicted model), `bin/mini-ork-execute:289-319,352,1665-1737,1808-1866` (reward + region), `bin/mini-ork-classify:90,129,181-200,219,227,241` (classifier), `config/agents.yaml:16-57` (role/lens map), `config/providers.yaml:47-63` (model pins), `mini_ork/dispatch/providers.py:28-36,158-167,269-299` (provider/cwd), `mini_ork/cost_advisor.py:81-201` (advisor), `lib/pattern_store.sh:99,167-230` (patterns), `lib/context_assembler.sh:443-486` (prior runs context), `lib/topology.sh:127-136` (topology), `lib/rho_aggregator.sh:108-111` (rho), `lib/cw_por.sh`, `lib/adaptive_stability.sh`, `lib/krippendorff_alpha_gate.sh`, `lib/policy_store.sh` (gates/policy), `bin/mini-ork-conductor:204-222` (plasticity budget).

### From lens-kimi (`lens-kimi.md`)
- File anchors: `mini_ork/lane_router.py:286-330,195-199,270-279`; `mini_ork/ported/decision_service.py:98-309`; `mini_ork/trace_store.py:28-40,195-223`; `lib/lane_router.sh:8-18,173-187`; `bin/mini-ork-execute:453-509,753-754`; `mini_ork/optimize/gepa.py:55-91`; `mini_ork/web/routes/trajectory.py:170`; `run_detail.py:295,310,375`; `bin/mini-ork-reflect:184,301`; `bin/mini-ork-metrics:105`; `tests/unit/test_lane_router.sh:78`; `tests/test_gepa_wiring_py.py:215`; `mini_ork/ported/cost_pause.py:24-57`.
- Papers (≥10 inline): [arxiv:2507.19457] GEPA; [arxiv:2402.03300] GRPO/DeepSeekMath; [arxiv:1707.06347] PPO; [arxiv:2402.14740] RLOO; [arxiv:2501.03262] REINFORCE++; [arxiv:2606.16733] first-principles PG; [arxiv:2603.01162] GRPO as U-statistic; [arxiv:2502.10985] Elo reliability; [arxiv:2503.12020] variance-dependent bandit regret; [arxiv:2502.05145] finite-horizon bandits; [arxiv:2310.03714] DSPy; [arxiv:2406.07496] TextGrad; [arxiv:2303.11366] Reflexion; [arxiv:2605.29463] reflexive confabulation. Plus classics: (Elo 1978), (Herbrich, Minka & Graepel 2007), (Williams 1992), (Auer, Cesa-Bianchi & Fischer 2002), (Langford & Zhang 2008).

### From lens-codex (`lens-codex.md`)
- Public framework repos (commit-pinned): [`langchain-ai/langgraph@1.2.9`](https://github.com/langchain-ai/langgraph/releases/tag/1.2.9); [`microsoft/autogen@027ecf0a`](https://github.com/microsoft/autogen/commit/027ecf0a379bcc1d09956d46d12d44a3ad9cee14); [`OpenHands/OpenHands@808eb06`](https://github.com/All-Hands-AI/OpenHands/commit/808eb06bb29f5ef5dbcfc1e7bc67565bc8d20b0f); [`SWE-agent/SWE-agent@1132b3e`](https://github.com/SWE-agent/SWE-agent/commit/1132b3e80a45487ce8423f75d0e180874bf84caa); [`FoundationAgents/MetaGPT@11cdf46`](https://github.com/geekan/MetaGPT/commit/11cdf466d042aece04fc6cfd13b28e1a70341b1f); [`Significant-Gravitas/AutoGPT@e2711b1`](https://github.com/Significant-Gravitas/AutoGPT/commit/e2711b1748bdc3fe702ab4e44c6a11df98458c53); [`letta-ai/letta@b76da90`](https://github.com/letta-ai/letta/commit/b76da9092518cbaa2d09042e52fdcbde69243e18); [`mem0ai/mem0@df9d5cc`](https://github.com/mem0ai/mem0/commit/df9d5cc4b151861304bb4f7ec1fdca6d54bbc45a); [`run-llama/llama_index@7fd33e0`](https://github.com/run-llama/llama_index/commit/7fd33e00a8947183327e75aef14687c499d5c150); [`agno-agi/agno@2b2081f`](https://github.com/agno-agi/agno/commit/2b2081fa8a5fa8a0825b381f29e49a3d738447d3); [`camel-ai/camel@c448d94`](https://github.com/camel-ai/camel/commit/c448d94b6268f6dcbba3f34cf36085066530a0d5).
- Negative controls (excluded): Cursor Background Agents, Devin, CrewAI, Semantic Kernel.

### From lens-opus (`lens-opus.md`)
- File anchors: `bin/mini-ork-execute:289-295,352,660,651,1665-1737,1735,1808-1866`; `bin/mini-ork-spawn:113,123`; `recipes/doc-to-features-loop/lib/per_feature_dispatcher.py:422`; `lib/lane_router.sh:121,226,432,500,511-545`; `lib/decision_service.sh:121`; `lib/context_assembler.sh:443-486` (especially `:457-469`); `lib/topology.sh:127-136`; `lib/rho_aggregator.sh:108-111`; `lib/process_reward.sh:1-20`; `mini_ork/trace_store.py:125`; `lib/trace_store.sh:196`; `docs/architecture/techniques-compendium.md:121`; `db/migrations/0042_execution_traces_objective_aware_reward.sql:9`; `db/migrations/0043_lane_domain_advantage.sql`; `kickoffs/miniork-intelligence-audit.md:20-23`; `bin/mini-ork-usage-report:281`; `lib/recursive_orchestration.sh:143-155`; `bin/mini-ork-reflect:301`.
- External citations: [arxiv:2210.03629] ReAct (Yao 2022); [arxiv:2310.08560] MemGPT (Packer 2023); [arxiv:2305.16291] Voyager (Wang 2023); [arxiv:2202.05780] meta-learning (Kirsch & Schmidhuber 2022); [arxiv:2304.11406] LaMP (Salemi 2024); [arxiv:2209.13085] reward hacking (Skalse 2022); LangGraph persistence overview; Efron & Morris 1975 (shrinkage); `internal-docs/research/2026-07-03-adding-eval-to-miniork-run-flow.md`.

### Synthesis judgments (added in this doc)
- Combines the four lenses; no new external citations introduced.
- Recommendation #3 design follows Opus §5 exactly; #1 follows GLM-12 with Kimi §6 caveat applied; #5 follows Opus recommendations #1+#6 with Kimi-7 + Codex convergent pattern #1.

---

## Appendix A — INTELLIGENT (closed-loop) inventory

The full list of mechanisms that satisfy the write→read→decision criterion (Kimi §1 criteria 1+3+4; GLM Catalog B LIVE tags):

| # | mechanism | write site | read site | loop status |
|---|-----------|------------|-----------|-------------|
| 1 | GRPO lane routing | `mini_ork/lane_router.py:235-279` (`agent_performance_memory` UPSERT; `lane_*_advantage` UPSERT) | `mini_ork/lane_router.py:286-330` (`preferred_lane`); consumed at `mini_ork/ported/decision_service.py:284-287`, `lib/decision_service.sh:80-217` | **INTELLIGENT** (structure) / **EFFECTIVELY-STATIC** (reward starvation + floor noise) — Dispute 1 |
| 2 | Gradient → context_assembler | `lib/gradient_extractor.sh:57,97,383` (`gradient_records` insert) | `mini_ork/context_assembler.py:118,249`; `lib/context_assembler.sh:143,374` (failure-mode injection) | **INTELLIGENT** (closed) |
| 3 | Gradient → role-evolver | `lib/gradient_extractor.sh:57,97,383` | `lib/role_evolver.sh:114` (proposals) | **INTELLIGENT** (closed) |
| 4 | Pattern → reflection chain | `lib/pattern_store.sh:99,167-230` (`pattern_records`) | `lib/reflection_pipeline.sh:320,494` (cluster→promotion summarizer) | **INTELLIGENT** (closed upstream of emergent_patterns) |
| 5 | GEPA within-run | `mini_ork/optimize/gepa.py:62` (in-memory `ParetoFront._entries`) | `mini_ork/optimize/gepa.py:55-83` (`select`) | **INTELLIGENT (within-run) / AMNESIC (cross-run)** — no persistent archive |
| 6 | Topology win-rate → conductor | `lib/topology.sh` (recipe-topology rollup) | `bin/mini-ork-conductor:139,154` (topology win_rate base for predicted score) | **INTELLIGENT** (single consumer) |

---

## Appendix B — NOT-INTELLIGENT inventory

### B.1 STATIC (config constants, no data input)

54 entries in GLM Catalog A; key structural ones (file:line — what it is):

- PRM weights `lib/process_reward.sh:99-100` / `mini_ork/learning/process_reward.py:55-61` — `0.40/0.20/0.10/0.15/0.10/0.05/0.15` linear blend, dual-source-of-truth
- Verdict vocabulary `mini_ork/learning/process_reward.py:63` — frozen set `{"approve","approved","pass","success","ok"}`
- GRPO hyperparameters `mini_ork/lane_router.py:35-38` / `lib/lane_router.sh:87-90` — `SHRINKAGE_K=5, DECAY_ALPHA=0.30, HALFLIFE_DAYS=14`
- Sample floor `mini_ork/lane_router.py:291` / `lib/lane_router.sh:500` — `MO_LEARNING_MIN_SAMPLES=3`
- Explore rate `lib/decision_service.sh:121` — `MO_LEARNING_EPSILON=0.10` constant, no decay
- Default lane fallback `lib/decision_service.sh:105` — `route=$(decision_service_default_lane "$node_type")`
- Family map `lib/decision_service.sh:263` — `LANE_TO_FAMILY={...}` hardcoded
- Status→reward map `bin/mini-ork-execute:289-299` — 3-level step function `1.0/0.0/0.5`
- Fallback chains `bin/mini-ork-execute:312-324` — `MO_FALLBACK_CODING`, `MO_FALLBACK_REVIEW`, `MO_FRONTIER_LANE`, `MO_CHEAP_LANE`
- Reward anchor `bin/mini-ork-execute:1809` — `MO_REWARD_ANCHOR:-0.5`
- Cost advisor tiers `mini_ork/cost_advisor.py:81-90` — model+context map, budget thresholds, default lane
- Role→lane map `config/agents.yaml:16-31` — planner/researcher/implementer pinned
- Lens→family map `config/agents.yaml:41-53` — 5-family panel pinned
- Budget caps `config/agents.yaml:55-57` / `bin/mini-ork-conductor:48` / `lib/llm-dispatch.sh:1377` — `$50/day` triple-hardcoded
- Classifier keywords `bin/mini-ork-classify:181-241` — `+1/+2/+3` word bonuses, no embedding
- Plasticity budget `bin/mini-ork-conductor:204-222` — daily mutation cap `5`, predicted-score gain `0.3`
- Error taxonomy `lib/llm-dispatch.sh:1279-1295,168-205` — frozen regex/keyword table
- Gates `lib/cw_por.sh` / `lib/adaptive_stability.sh` / `lib/krippendorff_alpha_gate.sh` — fixed thresholds
- Recursion/depth `mini_ork/ported/decision_service.py:265-275` — pure env read, no data input

### B.2 WRITE-ONLY (rows produced and counted, no decision reads them)

| # | table | write site | why it's not consulted |
|---|-------|-----------|------------------------|
| 1 | `emergent_patterns` | `lib/reflection_pipeline.sh:396-437` (status='proposed') | no `SELECT … emergent_patterns` in routing, decision, or context_assembler paths (GLM-12) |
| 2 | `prompt_win_rates` | `lib/rho_aggregator.sh` / `mini_ork/ported/rho_aggregator.py` | only `rho_top_prompts` + lifetime reporting; `bin/mini-ork-conductor:9` *claims* to read it but body queries only `topology_win_rates` + `agent_performance_memory` (GLM-12, header is stale) |
| 3 | `conductor_decisions.outcome` / `realized_score` | `bin/mini-ork-conductor:248-256` | only `scripts/smoke-learning-loops.sh:142` (asserts `success:1.0`); conductor's predicted-score model never regresses on it |
| 4 | Reflection chain (w.r.t. routing) | `lib/gradient_extractor.sh:57`; `lib/reflection_pipeline.sh:266`; `bin/mini-ork-reflect:301,184`; `bin/mini-ork-metrics:105` | readers are UI counts (`mini_ork/web/routes/trajectory.py:170`, `run_detail.py:295,310,375`) + tests; no `decide()`/`lane_router` reader by key (Kimi-5) |
| 5 | `bug_reports` (emission half) | `lib/bug_report.sh` + `bin/mini-ork-bug-collector` (regex/severity/confidence) | ingestion is LIVE; emission is regex heuristic (same shape as `mini_ork-classify`), precision bounded by hand-written patterns |

### B.3 DEAD (write site exists, no in-repo reader found)

| # | table | write site | why it's dead |
|---|-------|-----------|---------------|
| 1 | `run_artifact_edges` | insert site exists | grep across `*.sh` + `*.py` returned zero non-test readers (GLM-12); also absent from context_assembler, conductor, lane_router, web routes |

### B.4 NOT-INTELLIGENT veneer (looks intelligent but isn't)

| # | mechanism | file:line | why it isn't |
|---|-----------|-----------|--------------|
| 1 | `coalition_ok` field in `decide()` return | `mini_ork/ported/decision_service.py:123-197` | computed AFTER `route` is fixed at `:287`; output-only diagnostic, not a decision input |
| 2 | `reward_summary` field in `decide()` return | `mini_ork/ported/decision_service.py:200-262` | same — computed after route is fixed; no external caller branches on it |
| 3 | `cost_pause` sidecar | `mini_ork/ported/cost_pause.py:24-57` | self-read same run only; within-run accounting, not cross-run learning |

---

## Appendix C — Top 5 highest-leverage changes (in order)

Synthesizing across all 4 lenses, ranked by combined structural + operational payoff:

1. **Promote verified `emergent_patterns` into `context_assembler`'s failure-mode injection path.** Single biggest gap between "the system looks reflective" and "reflection changes routing." Pre-condition: build extract→distill→verify pipeline (Kimi §6 caveat). *(GLM-12 highest-leverage #2 + Kimi-5 + Opus-6 #4.)*

2. **Read `conductor_decisions.realized_score` back into the conductor's predicted-score model** (replace the fixed `0.3` gain with fitted/EMA coefficient). The meta-orchestrator currently cannot learn from its own prediction error. *(GLM-12 highest-leverage #1 + Opus Case A.)*

3. **Replace `_mo_reward_from_status` with a graded signal OR guarantee `reward_g` population via eval node.** Without this, the GRPO rail is starved; reward_g=NULL for most runs makes the lane router operate on a thin slice. *(GLM-12 highest-leverage #5 + Kimi-3 + Opus-6 #4.)*

4. **Document `MINI_ORK_OBJECTIVE_DOMAIN` as a feature-scoping lever and have the recursive spawn path inherit parent_run_id → objective_domain automatically.** Makes the lever opt-in-by-omission rather than opt-in-by-discipline. *(Opus-6 #1+#6 + Kimi-7 + Codex convergent pattern #1.)*

5. **Run the Opus §5 η² experiment on existing `state.db` data** before investing in feature-scoping infrastructure. SQL-only, no code changes. Resolves the entire "is feature-isolation worth building" debate with data. *(Opus §5 + Kimi-7 + GLM-7.)*