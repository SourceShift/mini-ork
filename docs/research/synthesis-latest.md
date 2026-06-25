# Research Synthesis — GATE FAILURE (DO NOT PUBLISH)

> **STATUS: ❌ PRE-SYNTHESIS ARTIFACT GATE FAILED.**
> The four-lens contract for this `research_synthesis` recipe is **not**
> satisfied. **1 of 4 required lens artifacts is missing.** This file is a
> **fail-closed report**, not a finalized synthesis. The
> `source-completeness` verifier MUST exit non-zero and the publisher MUST
> NOT update `docs/research/synthesis-latest.md`.

**Research question:** Are process reward models (PRM, per-step rewards)
actually superior to outcome reward models (ORM, end-of-trajectory rewards)
for training and routing multi-step LLM agents?

**Run:** `run-1782213983-39383`

---

## 0. Why this run failed closed

| Lens | Role | File | Status |
|------|------|------|--------|
| GLM  | BREADTH — recent web/vendor/news/practitioner sources | `lens-glm.md` | ❌ **MISSING** |
| Kimi | RIGOR — academic literature | `lens-kimi.md` | ✅ present (204 lines, 15 papers) |
| Codex| PRACTICE — code-pattern survey | `lens-codex.md` | ✅ present (200 lines, 11 repos) |
| Opus | THEORY — deep narrative analysis | `lens-opus.md` | ✅ present (221 lines, 11 cites) |

**Root cause:** the GLM lane never produced an artifact. Its provider call
returned an HTTP **429** before emitting any content:

```
llm-failures/1782214274-glm.out
  api_error_status: 429
  "[1313] Your account's current usage pattern does not comply with the
   Fair Usage Policy, and your request frequency has been limited."
```

This is a **known, recorded failure mode** for this repo (glm 429 "Fair
Usage" silently sinks runs — throttle-guard blind spot). It is exactly the
class of failure prior `research_synthesis` runs were caught publishing
through (missing/placeholder lens reaching synthesis). Per the learned
failure modes and the plan's `hard-pre-synthesis-gate`, the correct action
is to **fail before synthesis**, not to fabricate or back-fill the BREADTH
lens, and not to vote a 3-lens result into a 4-lens conclusion.

**What is lost by the missing lens:** GLM was the only BREADTH lane —
recent (2025–2026) vendor narratives, product/blog claims, benchmark
chatter, and practitioner anecdote. The remaining three lenses are heavily
**academic + code-centric**, so any "what is industry actually saying right
now" signal is unrepresented. Consensus markers below therefore top out at
**★★ (2-lens)**; no ★★★ (all-4) consensus is computable for this run.

**Required remediation before a valid synthesis can be published:**
re-dispatch the GLM lens (or substitute a distinct model-family BREADTH
lane per the diversity invariant — NOT by collapsing it onto kimi/codex/
opus), export `MINI_ORK_SECRETS` if running foreign-home, and re-run the
pre-synthesis gate. Only then may the reviewer-synthesizer and publisher
proceed.

---

## ⚠️ PROVISIONAL 3-LENS DIGEST (NON-AUTHORITATIVE)

Everything below is a **provisional reading of the 3 lenses that did
exist**, provided for the human operator only. It is **not** the recipe's
deliverable, **must not** be published, and is explicitly **missing the
BREADTH lens**. Treat confidence ceilings as capped accordingly.

### Section 1: TL;DR (provisional, ≤5 bullets)

1. **PRM's clearest, best-replicated win is inference-time selection
   (best-of-N re-ranking), not RL training reward.** (Opus-§2/§6 +
   Kimi-1.1; lens-codex corroborates that shipped code rarely trains on
   PRMs) — **confidence: high** (within the 3 available lenses).
2. **At frontier training scale, outcome/verifiable reward with a group
   baseline (GRPO / RLVR) is the shipped default, and at least one frontier
   lab tried PRMs and dropped them for reward-hacking + cost reasons.**
   (Opus-§3 + Kimi-2.2 DeepSeek-R1) — **confidence: high.**
3. **The PRM-vs-ORM dichotomy may be partly false:** GRPO with outcome
   rewards is argued to be mathematically equivalent to an implicit
   step-level (PRM-like) objective. (Kimi-2.3 arxiv:2509.21154; Opus-§3
   makes the same "the line blurs" argument) — **confidence: medium**
   (single theory paper, awaiting independent replication).
4. **Public production code overwhelmingly emits a terminal scalar /
   verifiable / pass-fail signal; explicit agent PRMs are research add-ons
   with weak adoption.** (Codex convergent patterns 1–5) — **confidence:
   high for "what ships," not for "what is scientifically best."**
5. **Imperfect verifiers — process OR outcome — get hacked, and more
   test-time compute can make it worse.** (Kimi-4.1/4.2 + Opus-§4) —
   **confidence: medium-high.**

### Section 2: Consensus findings (≥2 of 3 available lenses)

> Markers capped at ★★ because only 3 lenses ran. ★ = 2-lens, ★★ = all 3
> available lenses. **No ★★★ is possible this run.**

- **★★ PRM dominates for selection/verification on hard math, ORM-style
  signals dominate for scalable RL training.** (Opus-§2/§6 + Kimi-1.1/2.1 +
  Codex "verifiable rewards beat learned PRMs in public code"). All three
  lenses independently split the question by *regime* (selection vs.
  training) rather than declaring a global winner.
- **★★ Automatic PRM labels (Monte-Carlo rollout-derived) are noisy and
  degrade toward a high-variance ORM on long horizons.** (Opus-§4 +
  Kimi-4.3 Qwen "Lessons" / Kimi-3.1 ProcessBench; Codex §11 Math-Shepherd
  "ORM-derived at labeling time"). 
- **★★ Reward hacking is a first-order reason to distrust learned reward
  models in RL loops.** (Opus-§4 Skalse 2022 + Kimi-2.2 DeepSeek-R1 +
  Kimi-4.1 Helff 2026).
- **★ (2-lens) The PRM/ORM boundary collapses once you use process
  *advantage* / group-relative baselines rather than step *correctness*.**
  (Opus-§3 Setlur PAVs + Kimi-2.3 "GRPO is secretly a PRM").
- **★ (2-lens) PRM results are math-domain-specific and transfer to agentic
  domains is unproven / under-evaluated.** (Opus-§4/rec-7 + Kimi-3.2/5.4
  AgentPRM, Kimi-caveat-3).

### Section 3: Disputed findings (NOT vote-ruled)

1. **"PRMs beat ORMs on the metric that matters."**
   - *Pro:* Kimi-1.1 (Lightman 2023: 78.2% vs 72.4% best-of-N on MATH);
     Kimi-5.x (R-PRM, ThinkPRM, BiRM all report PRM gains).
   - *Con:* Opus-§3 (Uesato 2022 found *comparable* final-answer accuracy,
     PRM's real win was fewer reasoning errors); Kimi-2.2 (DeepSeek-R1
     reaches SOTA with no neural reward model at all).
   - *Judgment:* **different regime + different metric.** Pro-PRM numbers
     are mostly *selection/best-of-N* on *math*; con evidence is *training*
     reward at *scale*. They are not measuring the same thing. This is a
     genuine, unresolved dispute — do not average it.
   - *Resolver:* a compute-equalized head-to-head (equal reward-model FLOPs
     + rollout budget) separating the *training* arm from the *selection*
     arm on a non-math agentic benchmark (Opus-§5 proposes exactly this).

2. **"Better PRM architectures fix the problem" vs. "the concept is the
   problem."**
   - *Pro-fixable:* Kimi-5.1/5.2/5.3 (R-PRM, ThinkPRM, BiRM — generative/
     bidirectional PRMs widen the gap; ThinkPRM needs only ~8K labels).
   - *Pro-abandon:* Kimi-2.2 + Opus-§3 (DeepSeek dropped PRMs as ill-posed
     and hackable).
   - *Judgment:* unresolved; the "fix" papers are each single-study with
     released code but no independent replication on agentic (non-math)
     tasks. **Note: this dispute lives almost entirely inside one lens
     (Kimi) — the missing BREADTH/GLM lens would be the natural place to
     check whether industry practitioners corroborate either camp. Its
     absence weakens our ability to adjudicate.**

### Section 4: Cross-lens gaps (what NO available lens covered)

- **Industry/vendor present-tense signal — STRUCTURALLY MISSING** because
  the GLM BREADTH lens failed. No 2025–2026 product/blog/benchmark-chatter
  evidence is represented at all.
- **Routing** (the kickoff explicitly asked about *routing* multi-step
  agents). All three lenses cover training + verification well; *inference-
  time routing/orchestration reward signals* are thin (Opus-rec-6 and
  Codex-MASPRM touch it; no benchmark-grade evidence).
- **Cost accounting in dollars** rather than FLOPs/labels — nobody gives a
  $-per-quality-point comparison.
- **Cross-domain PRM transfer** (math → code → web/tool agents) — flagged
  as open by both Opus and Kimi; no settled result.

### Section 5: Numbered recommendations (PROVISIONAL — pending BREADTH lens)

1. **Use PRMs for inference-time best-of-N selection, not as the primary RL
   training reward for general agents.** (Opus-rec-1 + Kimi-1.1 +
   Codex-convergent-3). *Wrong if:* a compute-equalized study shows PRM RL
   beating baselined-ORM RL on an agentic benchmark.
2. **Default to outcome/verifiable reward with a strong group baseline
   (GRPO/RLVR) for training multi-step policies.** (Opus-rec-2 + Kimi-2.1/
   2.2 + Codex verl/TRL/OpenRLHF). *Wrong if:* group baselining adds no
   value over raw sparse reward at scale.
3. **If you adopt a process signal, use process *advantage* (progress),
   not step *correctness*.** (Opus-rec-3 + Kimi-2.3/5.4). *Wrong if:*
   correctness-labeled PRMs match advantage-PRMs at equal labeling cost.
4. **Budget for reward-hacking monitoring whenever a learned reward model
   sits in an RL loop; prefer rule-based verifiers where outcomes are
   checkable.** (Opus-rec-4 + Kimi-4.1/4.2). *Wrong if:* learned PRMs in
   long-horizon RL show no measurable hacking.
5. **Do not assume math PRM results transfer to tool/web/coding agents;
   re-validate "correct step" per domain.** (Opus-rec-7 + Kimi-caveat-3 +
   Codex SWE-agent/OpenHands separation of runtime from reward). *Wrong
   if:* a single PRM transfers zero-shot across math/code/web.
6. **[META] Re-run this synthesis with a real BREADTH lens before acting on
   the above as "research-validated."** The current digest is academic+code
   only. (synthesis judgment). *Wrong if:* the operator explicitly accepts
   a 3-lens academic/code-only reading as sufficient.

### Section 6: Source manifest (3 available lenses only)

**Opus (THEORY):**
- arxiv:2305.20050 (Lightman 2023, Let's Verify Step by Step / PRM800K)
- arxiv:2312.08935 (Wang 2024, Math-Shepherd)
- arxiv:2402.03300 (Shao 2024, DeepSeekMath / GRPO)
- arxiv:2406.06592 (Luo 2024, OmegaPRM)
- arxiv:2410.08146 (Setlur 2024, Process Advantage Verifiers)
- arxiv:2501.12948 (DeepSeek-AI 2025, DeepSeek-R1)
- arxiv:2211.14275 (Uesato 2022, process- vs outcome-based feedback)
- arxiv:2209.13085 (Skalse 2022, reward hacking)
- arxiv:2110.14168 (Cobbe 2021, GSM8K verifiers)
- Ng/Harada/Russell 1999; Amodei et al. 2016

**Kimi (RIGOR):**
- arxiv:2305.20050, 2312.08935, 2402.03300, 2501.12948 (overlaps Opus)
- arxiv:2509.21154 (Sullivan 2026, "GRPO is Secretly a PRM")
- arxiv:2412.06559 (Zheng 2025, ProcessBench)
- arxiv:2506.00027 (Wang 2025, Generalization of PRMs)
- arxiv:2604.15149 (Helff 2026, LLMs Gaming Verifiers)
- arxiv:2604.13602 (Wang 2026, Reward Hacking in the Era of Large Models)
- arxiv:2501.07301 (Qwen 2025, Lessons of Developing PRMs)
- arxiv:2503.21295 (She 2025, R-PRM)
- arxiv:2504.16828 (Xie 2025, ThinkPRM)
- arxiv:2503.04618 (Chen 2025, BiRM)
- arxiv:2511.08325 (Li 2025, AgentPRM)
- arxiv:2412.16720 (OpenAI 2024, o1 System Card)
- arxiv:2510.08049 (Zheng 2026, Survey of PRMs)

**Codex (PRACTICE):**
- github:OpenRLHF/OpenRLHF (experience_maker.py:1192/1279/1365)
- github:verl-project/verl (core_algos.py:337/348; reward_manager/naive.py:61)
- github:huggingface/trl (grpo_trainer.py, reward_trainer.py)
- github:RLHFlow/RLHF-Reward-Modeling, RLHFlow/Online-RLHF
- github:sanjibanc/agent_prm (README.md:297/316/380)
- github:milad1378yz/MASPRM (README.md:237/256)
- github:SWE-agent/SWE-agent; github:SWE-bench/SWE-bench (grading.py)
- github:OpenHands/OpenHands
- PRM800K / Math-Shepherd public artifacts (lookup-pending in run)

**GLM (BREADTH): — NONE. Lens failed (429). This manifest is incomplete by
design.**

---

*End of fail-closed report. A valid 4-lens synthesis requires re-dispatching
the GLM BREADTH lens and re-passing the pre-synthesis artifact gate.*
