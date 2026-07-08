# Research Synthesis: SIA vs mini-ork / TraceOtter / ContextNest / Coevolve

## Section 1: TL;DR (≤ 5 bullets)

- **SIA closes a narrow, evaluator-gated self-improvement loop (harness edits + optional weight updates) and the code is shipped, but its headline benchmark gains are vendor-reported and not independently replicated.**
  - Lenses: lens-glm source-01, lens-kimi P1, lens-codex impl-1/2, lens-opus §2.
  - Confidence: high.
- **SIA's weights mode is not a built-in trainer; it delegates to a generated `train.py` that depends on Tinker / Modal / SandboxFusion credentials, making it the highest-risk claim-vs-code surface.**
  - Lenses: lens-kimi P1, lens-codex impl-2/3, lens-opus §3.
  - Confidence: high.
- **Our stack is stronger on cross-run memory, provenance, multi-lane routing, and productized orchestration, but still lacks a real graded per-run eval node and has a fragile research-synthesis verifier.**
  - Lenses: lens-codex impl-4/5/6/9/12/13/14, lens-kimi P2/P5/P11, lens-opus §3/§4.
  - Confidence: high.
- **For continual, multi-task learning, memory-based RL (JitRL / Memento / SKILL-DISCO) is a methodologically credible alternative to SIA's per-task fine-tuning, but the two are not directly benchmarked against each other.**
  - Lenses: lens-kimi P2/P5/P9/P10, lens-glm source-07, lens-opus §3.
  - Confidence: medium-high.
- **The highest-leverage steal from SIA is its `evaluate.py → results.json → feedback-agent` contract, ported into mini-ork as a `type: eval` recipe node that feeds the already-shipped `reward_g` / lane-router / TraceOtter pipeline.**
  - Lenses: lens-codex impl-1/4/5, lens-kimi P11, lens-opus §6 rec-1/8.
  - Confidence: high.

---

## Section 2: Consensus Findings

### SIA strengths vs us

- ★★★ **SIA has a concrete, generation-level evaluator contract.** The orchestrator runs the target artifact, then a task-local `evaluate.py` writes `results.json`, and the feedback prompt consumes that scalar evidence to mutate the next generation. This is a falsifiable, file-system-level improvement signal that our stack has not yet made first-class.
  - Evidence: lens-codex impl-1 (`/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:185`, `/Volumes/docker-ssd/ps/sia/EVALUATION_GUIDE.md:5`); lens-kimi P1 (`/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:185-270`); lens-opus §2 (`/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:185`).

- ★★★ **SIA is narrower and more benchmark-shaped, which makes its loop easier to report and replicate (in principle).** It accepts one task, one public/private data split, one generated artifact, and one metric. This matches the eval-driven-development culture better than our broad orchestration graph.
  - Evidence: lens-codex impl-1/2 (`/Volumes/docker-ssd/ps/sia/sia/cli.py:84`, `/Volumes/docker-ssd/ps/sia/sia/config.py:30`); lens-kimi P1; lens-opus §2.

- ★★ **SIA treats harness edits and weight updates as complementary levers.** The CLI exposes `--focus harness` and `--focus weights`; weights mode switches the generated artifact to `train.py`. The literature cluster around SIA (Self-Rewarding LMs, Voyager, STaR) supports combining improvement levers rather than choosing one.
  - Evidence: lens-glm source-01/source-19; lens-codex impl-2 (`/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:661`, `/Volumes/docker-ssd/ps/sia/sia/prompts.py:432`); lens-opus §2.

### Our strengths vs SIA

- ★★★ **Our stack has shipped cross-run operational learning substrate that SIA lacks.** mini-ork records `reward_value` / `reward_g` on every node trace, computes relative lane advantage, and stamps execution-time rewards; TraceOtter distills histories into training datasets; ContextNest stores provenance-graded memory; Coevolve exposes the operator shell.
  - Evidence: lens-codex impl-4/5/6/10/11/12/13/14 (`/Volumes/docker-ssd/ps/mini-ork/mini_ork/trace_store.py:28`, `/Volumes/docker-ssd/ps/mini-ork/mini_ork/lane_router.py:31`, `/Volumes/docker-ssd/ps/TraceOtter/traceotter/pipeline.py:15`, `/Volumes/docker-ssd/ps/ContextNest/src/ingest/claude_code/extractor.rs:20`, `/Volumes/docker-ssd/ps/coevolve/internal/core/integration.go:9`); lens-kimi P2/P5/P11; lens-opus §3/§4.

- ★★★ **Our stack is methodologically stronger for continual learning and avoids catastrophic forgetting by design.** JitRL proves a memory-only additive logit update is sufficient and 30× cheaper; Memento and SKILL-DISCO corroborate memory-based adaptation; SIA overwrites `target_agent.py` per generation with no non-erasing archive.
  - Evidence: lens-kimi P2/P5/P9 (`/Volumes/docker-ssd/ps/TraceOtter/docs/roadmap/m9-continual-jit-learning.md`); lens-glm source-07/source-08; lens-opus §3; lens-codex convergent pattern 1.

- ★★ **Our stack records process telemetry and provenance, not just final benchmark scores.** ContextNest grades verification/read-context/feature claims against raw tool events; mini-ork traces include cost, duration, tool calls, and rubric scores. This reduces the risk of climbing the wrong hill.
  - Evidence: lens-codex impl-12/13 (`/Volumes/docker-ssd/ps/ContextNest/src/ingest/claude_code/extractor.rs:1129`, `/Volumes/docker-ssd/ps/ContextNest/src/ingest/claude_code/extractor.rs:1308`); lens-kimi P4/P6; lens-opus §4.

- ★★ **Our stack has a productized control plane (Coevolve) that SIA does not.** Coevolve registers orchestrator / memory / learning / worker integrations and renders real run/cost/router/context panels rather than benchmark dashboards.
  - Evidence: lens-codex impl-14/15 (`/Volumes/docker-ssd/ps/coevolve/internal/core/integration.go:133`, `/Volumes/docker-ssd/ps/coevolve/internal/run/controller.go:82`); lens-opus §3.

---

## Section 3: Disputed Findings

### Dispute 1: Is SIA's narrow benchmark loop more "scientific" or just more limited?

- **Claim A (SIA is more scientific):** The SIA loop is short, falsifiable, and centered on a crisp evaluator; our broad orchestration stack optimizes many things but not one benchmark. (lens-glm source-01/source-09, lens-opus §2 conventional wisdom)
- **Claim B (SIA is limited, outer-system is more complete):** Real software delivery requires memory, routing, provenance, rollback, cost control, and operator surfaces; a benchmark score can miss all of them. (lens-kimi P4/P11, lens-opus §3 dissent, lens-codex impl-12/13/14)
- **Why they disagree:** Different definitions of "improvement." SIA optimizes a task-local scalar; our stack optimizes repeated heterogeneous work. The disagreement is a scope dispute, not a factual one.
- **Evidence that would resolve it:** A controlled cross-task transfer experiment: ten learning tasks + ten held-out transfer tasks, same budget, measuring next-task performance, cost, failure recovery, and provenance accuracy. (lens-opus §5)

### Dispute 2: Are per-task weight updates necessary, or is memory-based RL sufficient?

- **Claim A (weight updates are complementary and sometimes required):** Some domains (GPU kernels, legal charge classification, scRNA denoising) need latent skill acquisition that prompting/memory cannot provide. (lens-glm source-01, lens-opus §2 conventional wisdom)
- **Claim B (memory-based RL is the right default for continual systems):** JitRL proves a frozen base plus additive logit memory is sufficient, 30× cheaper, and avoids catastrophic forgetting; SIA's per-task fine-tuning assumes a closed task. (lens-kimi P2/P5, lens-glm source-07)
- **Why they disagree:** Different time horizon and task distribution. SIA assumes a fixed task with a clear eval endpoint; JitRL assumes a continuous stream of tasks.
- **Evidence that would resolve it:** Head-to-head ablation on the same task sequence: SIA `--focus weights` vs JitRL-style memory router, measuring final performance, forgetting, and cost.

### Dispute 3: Does SIA's stationary evaluator fatally cap its loop?

- **Claim A (evaluator is a strength):** SIA's `evaluate.py → results.json` contract turns improvement into an objective, legible signal. (lens-opus §2, lens-codex impl-1)
- **Claim B (stationary evaluator is a liability):** Red Queen Gödel Machine argues evaluators must co-evolve with the agent; a fixed evaluator can be gamed and caps attainable competence. (lens-kimi P4, lens-opus §4)
- **Why they disagree:** One side treats the evaluator as ground truth; the other treats it as another mutable artifact under adversarial pressure.
- **Evidence that would resolve it:** Measure SIA's gaming-resistance: introduce a held-out human or oracle judge and report divergence between `results.json` and the oracle.

---

## Section 4: Cross-Lens Gaps

1. **No controlled ablation of all three persistence modes.** No source compares weight updates, memory writes, and harness edits within one system on one task sequence. SIA compares harness-only / weights-only / combined, but does not include a memory-only arm. (lens-glm §"What's NOT", lens-kimi P2/P5)
2. **No independent replication of SIA's headline numbers.** LawBench 45% → 70.1%, 14× TriMul speedup, and scRNA gains are vendor-reported; no third-party lab reproduction was found. (lens-kimi P1 caveats, lens-opus §4)
3. **No cross-project transfer evidence.** Neither SIA nor our stack reports whether improvement on one task/package improves performance on unrelated real-repo delivery tasks. (lens-glm §"What's NOT", lens-opus §5)
4. **No operational-cost accounting.** Sandbox runs, evaluator calls, fine-tuning retries, trace storage, and human review gates are not priced in SIA's public claims; our stack also lacks a unified cost-of-learning ledger. (lens-glm §"What's NOT", lens-codex impl-8)
5. **No safety / gaming-resistance case.** The literature lacks a standard "agent self-improvement safety case" comparable to benchmark leaderboards. SIA does not measure evaluator gaming. (lens-kimi P4, lens-glm source-08/source-09, lens-opus §4)
6. **SIA Chinese-language internals not reviewed.** Design rationale, issues, or discussions in Chinese docs are a coverage gap. (lens-kimi caveats, lens-opus §4)
7. **ContextNest audit is incomplete.** The available checkout in this environment is `/Volumes/docker-ssd/ps/ML/ContextNest`; the Rust substrate service described in the plan lives at `/Volumes/docker-ssd/ps/ContextNest` and was only partially audited. (lens-opus §4 evidence gap)
8. **mini-ork research-synthesis verifier is fragile.** The workflow references a root `verifiers/source-completeness.sh` that is missing, and a similar older verifier exits 0 on failure. (lens-codex impl-9)

---

## Section 5: Numbered Recommendations

### Steal-from-SIA shortlist

This is the steal-from-SIA shortlist.

1. **Steal SIA's generation-level evaluator contract as a first-class `type: eval` recipe node.** After a mini-ork node produces an artifact, run a task-local `evaluate.py` that writes a structured `results.json`, then stamp the scalar(s) into `reward_value` / `reward_g` so the lane router can learn from it.
   - Supported by: lens-codex impl-1/4/5, lens-kimi P11, lens-opus §6 rec-1/8.
   - Anchor: `/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:185`, `/Volumes/docker-ssd/ps/sia/EVALUATION_GUIDE.md:5`, `/Volumes/docker-ssd/ps/mini-ork/internal-docs/research/2026-07-03-adding-eval-to-miniork-run-flow.md`.
   - **Wrong if:** our dominant tasks cannot define any meaningful held-out evaluator (lens-opus §6 rec-1).

2. **Add a held-out eval gate before recursive self-improvement promotion.** Reuse SIA's evaluator pressure, but require a dev/test split and a co-evolving rubric so the loop cannot overfit a single frozen metric.
   - Supported by: lens-kimi P4/P11, lens-opus §6 rec-4, lens-codex impl-7.
   - Anchor: `/Volumes/docker-ssd/ps/mini-ork/docs/RECURSIVE-SELF-IMPROVE.md`, `/Volumes/docker-ssd/ps/TraceOtter/docs/ENGINEER_WORKFLOW.md:77`.
   - **Wrong if:** the evaluation signal is too noisy to distinguish real improvement from variance (lens-opus §6 rec-4).

3. **Offer SIA-style harness mutation as an optional narrow-task recipe, separate from the general orchestration path.** Use it for benchmark-style tasks with clean evaluators; keep mini-ork's broad multi-lane scheduler as the default for messy product work.
   - Supported by: lens-codex impl-1/2, lens-opus §6 rec-1/8.
   - Anchor: `/Volumes/docker-ssd/ps/sia/sia/cli.py:84`, `/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:661`.
   - **Wrong if:** the project has no crisp benchmark tasks and all work is open-ended repo delivery (lens-opus §6 rec-8).

4. **Route SIA/TraceOtter weight updates to a late-stage reward consumer, not the first learning surface.** Build eval + quality gates first (TraceOtter curation, ContextNest provenance), then use SIA's prompt-embedded RL cookbook only after the harness is stable and reward data is clean.
   - Supported by: lens-kimi P2/P7/P8, lens-codex impl-3/10/11, lens-opus §6 rec-2/8.
   - Anchor: `/Volumes/docker-ssd/ps/sia/sia/prompts.py:432`, `/Volumes/docker-ssd/ps/TraceOtter/traceotter/curation.py:65`, `/Volumes/docker-ssd/ps/TraceOtter/docs/roadmap/m9-continual-jit-learning.md`.
   - **Wrong if:** the target domain has abundant clean reward data and the harness is already stable (lens-opus §6 rec-2).

5. **Co-evolve the evaluator/rubric with the agent to avoid stationary-evaluator caps.** Adopt the Red Queen Gödel Machine critique as a design requirement: every eval metric gets a shadow oracle or periodic adversarial audit.
   - Supported by: lens-kimi P4, lens-glm source-09/source-10, lens-opus §6 rec-4.
   - Anchor: `/Volumes/docker-ssd/ps/sia/sia/prompts.py:186` (warning about missing ground truth), `/Volumes/docker-ssd/ps/mini-ork/internal-docs/research/2026-07-05-arxiv-driven-rnd-loop.md`.
   - **Wrong if:** the evaluation task has a true, immutable ground truth and no gaming surface.

---

## Section 6: Source Manifest

### lens-glm

- [source-01] arxiv:2605.27276 — SIA: Self Improving AI with Harness & Weight Updates (Hebbar et al., 2026) — https://arxiv.org/abs/2605.27276
- [source-02] arxiv:2505.22954 — Darwin Gödel Machine (Zhang, Hu, Lu, Lange, Clune, 2025) — https://arxiv.org/abs/2505.22954
- [source-03] arxiv:2606.07412 — Socratic-SWE (Chuan Xiao et al., 2026) — https://arxiv.org/abs/2606.07412
- [source-04] arxiv:2511.13646 — Live-SWE-agent (Chunqiu Steven Xia et al., 2025) — https://arxiv.org/abs/2511.13646
- [source-05] arxiv:2512.18552 — Self-Play SWE-RL (Yuxiang Wei et al., 2025) — https://arxiv.org/abs/2512.18552
- [source-06] arxiv:2508.03680 — Agent Lightning (Xufang Luo et al., 2025) — https://arxiv.org/abs/2508.03680
- [source-07] arxiv:2506.10943 — Self-Adapting Language Models / SEAL (Adam Zweiger et al., 2025) — https://arxiv.org/abs/2506.10943
- [source-08] arxiv:2512.24873 — ALE / ROME (Weixun Wang et al., 2025) — https://arxiv.org/abs/2512.24873
- [source-09] arxiv:2410.10934 — Agent-as-a-Judge (Mingchen Zhuge et al., 2024) — https://arxiv.org/abs/2410.10934
- [source-10] arxiv:2604.18240 — AJ-Bench (Wentao Shi et al., 2026) — https://arxiv.org/abs/2604.18240
- [source-11] arxiv:2507.22844 — RLVMR (Zijing Zhang et al., 2025) — https://arxiv.org/abs/2507.22844
- [source-12] arxiv:2507.03112 — RLVER (Peisong Wang et al., 2025) — https://arxiv.org/abs/2507.03112
- [source-13] AlphaEvolve PDF — https://storage.googleapis.com/deepmind-media/DeepMind.com/Blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/AlphaEvolve.pdf
- [source-14] OpenEvolve — https://github.com/codelion/openevolve
- [source-15] arxiv:2407.01476 — Tree Search for Language Model Agents (Jing Yu Koh et al., 2024) — https://arxiv.org/abs/2407.01476
- [source-16] arxiv:2310.04406 — LATS (Andy Zhou et al., 2023) — https://arxiv.org/abs/2310.04406
- [source-17] arxiv:2310.02304 — STOP (Eric Zelikman et al., 2023) — https://arxiv.org/abs/2310.02304
- [source-18] arxiv:2305.16291 — Voyager (Guanzhi Wang et al., 2023) — https://arxiv.org/abs/2305.16291
- [source-19] arxiv:2401.10020 — Self-Rewarding Language Models (Weizhe Yuan et al., 2024) — https://arxiv.org/abs/2401.10020
- [source-20] arxiv:2308.00352 — MetaGPT (Sirui Hong et al., 2023) — https://arxiv.org/abs/2308.00352

### lens-kimi

- [P1] arxiv:2605.27276 — SIA (Hebbar et al., 2026)
- [P2] arxiv:2601.18510 — Just-In-Time Reinforcement Learning (JitRL)
- [P3] arxiv:2505.22954 — Darwin Gödel Machine
- [P4] arxiv:2606.26294 — Red Queen Gödel Machine
- [P5] arxiv:2508.16153 — Memento
- [P6] arxiv:2509.21154 — GRPO is Secretly a Process Reward Model (referenced, not re-verified)
- [P7] arxiv:2401.10020 — Self-Rewarding Language Models
- [P8] arxiv:2603.08660 — Unsupervised RLVR
- [P9] arxiv:2606.26669 — SKILL-DISCO (referenced via TraceOtter, not re-verified)
- [P10] arxiv:2606.06893 — Workflow-to-Skill (referenced via TraceOtter, not re-verified)
- [P11] arxiv:2411.13768 — Eval-Driven Development for LLM agents (referenced via mini-ork eval doc, not re-verified)
- [P12] arxiv:2305.16291 — Voyager
- File anchors: `/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:577`, `/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:822-835`, `/Volumes/docker-ssd/ps/sia/sia/config.py:74-79`, `/Volumes/docker-ssd/ps/sia/EVALUATION_GUIDE.md`, `/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:496-518`, `/Volumes/docker-ssd/ps/sia/sia/config.py:30`, `/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:312-352`, `/Volumes/docker-ssd/ps/TraceOtter/docs/roadmap/m9-continual-jit-learning.md`, `/Volumes/docker-ssd/ps/mini-ork/internal-docs/research/2026-07-03-adding-eval-to-miniork-run-flow.md`

### lens-codex

- Repository: github:hexo-ai/sia (local v0.5.1)
- Repository: github:SourceShift/mini-ork (commit f3c260be4fa069d3331eddb3ac4e676582a89e63)
- Repository: github:SourceShift/TraceOtter (commit 0ebb27d5420f8d34bb5617ee5f8b0db2a31ffb63)
- Repository: github:SourceShift/ContextNest (commit 1f1b2a245d68df3727b9e3f5fa848e9794d2000d)
- Repository: github:SourceShift/coevolve (commit 47a2aaead5f8956522187b1add8594180cc284fe)
- File anchors (SIA): `/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:661`, `/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:679`, `/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:692`, `/Volumes/docker-ssd/ps/sia/EVALUATION_GUIDE.md:5`, `/Volumes/docker-ssd/ps/sia/sia/cli.py:84`, `/Volumes/docker-ssd/ps/sia/sia/config.py:68`, `/Volumes/docker-ssd/ps/sia/sia/config.py:74-79`, `/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:821`, `/Volumes/docker-ssd/ps/sia/sia/prompts.py:39`, `/Volumes/docker-ssd/ps/sia/sia/prompts.py:51`, `/Volumes/docker-ssd/ps/sia/sia/prompts.py:186`, `/Volumes/docker-ssd/ps/sia/sia/prompts.py:236`, `/Volumes/docker-ssd/ps/sia/sia/prompts.py:433`
- File anchors (our stack): `/Volumes/docker-ssd/ps/mini-ork/mini_ork/trace_store.py:28`, `/Volumes/docker-ssd/ps/mini-ork/mini_ork/trace_store.py:67`, `/Volumes/docker-ssd/ps/mini-ork/mini_ork/trace_store.py:195`, `/Volumes/docker-ssd/ps/mini-ork/tests/unit/test_trace_store_py.py:72`, `/Volumes/docker-ssd/ps/mini-ork/mini_ork/lane_router.py:31`, `/Volumes/docker-ssd/ps/mini-ork/mini_ork/lane_router.py:90`, `/Volumes/docker-ssd/ps/mini-ork/lib/decision_service.sh:95`, `/Volumes/docker-ssd/ps/mini-ork/tests/unit/test_lane_router_py.py:60`, `/Volumes/docker-ssd/ps/mini-ork/bin/mini-ork-execute:1783`, `/Volumes/docker-ssd/ps/mini-ork/bin/mini-ork-execute:1801`, `/Volumes/docker-ssd/ps/mini-ork/bin/mini-ork:479`, `/Volumes/docker-ssd/ps/mini-ork/mini_ork/optimize/gepa.py:1`, `/Volumes/docker-ssd/ps/mini-ork/mini_ork/optimize/gepa.py:142`, `/Volumes/docker-ssd/ps/mini-ork/mini_ork/optimize/miniork_adapter.py:1`, `/Volumes/docker-ssd/ps/mini-ork/mini_ork/optimize/miniork_adapter.py:237`, `/Volumes/docker-ssd/ps/mini-ork/mini_ork/scheduler.py:79`, `/Volumes/docker-ssd/ps/mini-ork/mini_ork/scheduler.py:198`, `/Volumes/docker-ssd/ps/mini-ork/mini_ork/scheduler.py:232`, `/Volumes/docker-ssd/ps/mini-ork/recipes/research-synthesis/workflow.yaml:14`, `/Volumes/docker-ssd/ps/mini-ork/recipes/research-synthesis/workflow.yaml:20`, `/Volumes/docker-ssd/ps/mini-ork/recipes/research-synthesis/artifact_contract.yaml:4`, `/Volumes/docker-ssd/ps/mini-ork/recipes/mo-vs-omnigent/verifiers/source-completeness.sh:12`, `/Volumes/docker-ssd/ps/TraceOtter/traceotter/pipeline.py:15`, `/Volumes/docker-ssd/ps/TraceOtter/traceotter/pipeline.py:51`, `/Volumes/docker-ssd/ps/TraceOtter/traceotter/cli.py:91`, `/Volumes/docker-ssd/ps/TraceOtter/traceotter/cli.py:121`, `/Volumes/docker-ssd/ps/TraceOtter/traceotter/targets.py:13`, `/Volumes/docker-ssd/ps/TraceOtter/traceotter/targets.py:23`, `/Volumes/docker-ssd/ps/TraceOtter/traceotter/targets.py:75`, `/Volumes/docker-ssd/ps/TraceOtter/traceotter/targets.py:136`, `/Volumes/docker-ssd/ps/TraceOtter/traceotter/curation.py:65`, `/Volumes/docker-ssd/ps/ContextNest/src/ingest/claude_code/extractor.rs:20`, `/Volumes/docker-ssd/ps/ContextNest/src/ingest/claude_code/extractor.rs:1129`, `/Volumes/docker-ssd/ps/ContextNest/src/ingest/claude_code/extractor.rs:1246`, `/Volumes/docker-ssd/ps/ContextNest/src/ingest/claude_code/extractor.rs:1308`, `/Volumes/docker-ssd/ps/ContextNest/src/ingest/claude_code/extractor.rs:1338`, `/Volumes/docker-ssd/ps/ContextNest/src/api/prompt_context.rs:1`, `/Volumes/docker-ssd/ps/ContextNest/src/api/prompt_context.rs:84`, `/Volumes/docker-ssd/ps/ContextNest/src/api/substrate.rs:68`, `/Volumes/docker-ssd/ps/ContextNest/src/api/substrate.rs:176`, `/Volumes/docker-ssd/ps/ContextNest/src/api/sessions.rs:713`, `/Volumes/docker-ssd/ps/coevolve/internal/core/integration.go:9`, `/Volumes/docker-ssd/ps/coevolve/internal/core/integration.go:133`, `/Volumes/docker-ssd/ps/coevolve/internal/seams/db.go:30`, `/Volumes/docker-ssd/ps/coevolve/internal/seams/contextnest.go:81`, `/Volumes/docker-ssd/ps/coevolve/internal/run/controller.go:82`, `/Volumes/docker-ssd/ps/coevolve/internal/run/controller.go:161`, `/Volumes/docker-ssd/ps/coevolve/internal/run/controller.go:244`, `/Volumes/docker-ssd/ps/coevolve/internal/run/opencode_serve.go:83`, `/Volumes/docker-ssd/ps/coevolve/internal/run/opencode_serve.go:194`

### lens-opus

- arxiv:2605.27276 — SIA
- `/Volumes/docker-ssd/ps/sia/README.md:12`
- `/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:185`
- `/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:635`
- `/Volumes/docker-ssd/ps/sia/sia/orchestrator.py:821`
- `/Volumes/docker-ssd/ps/sia/sia/prompts.py:432`
- `/Volumes/docker-ssd/ps/mini-ork/mini_ork/trace_store.py:1`
- `/Volumes/docker-ssd/ps/mini-ork/mini_ork/lane_router.py:1`
- `/Volumes/docker-ssd/ps/mini-ork/docs/RECURSIVE-SELF-IMPROVE.md:3`
- `/Volumes/docker-ssd/ps/TraceOtter/docs/ENGINEER_WORKFLOW.md:3`
- `/Volumes/docker-ssd/ps/TraceOtter/docs/ENGINEER_WORKFLOW.md:77`
- `/Volumes/docker-ssd/ps/coevolve/README.md:18`
- `/Volumes/docker-ssd/ps/coevolve/README.md:55`
- `/Volumes/docker-ssd/ps/ML/ContextNest/memory/sessions/README.md:3`
- `/Volumes/docker-ssd/ps/ML/ContextNest/memory/agents/README.md:3`

### This synthesis additionally cites

- `/Volumes/docker-ssd/ps/mini-ork/internal-docs/research/2026-07-03-adding-eval-to-miniork-run-flow.md`
- `/Volumes/docker-ssd/ps/mini-ork/internal-docs/research/2026-07-05-arxiv-driven-rnd-loop.md`
- `/Volumes/docker-ssd/ps/TraceOtter/docs/roadmap/m9-continual-jit-learning.md`
