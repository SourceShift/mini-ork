# Why mini-ork — heterogeneous-family multi-agent, by construction

Most agentic frameworks ship multi-agent review and call it a day. mini-ork is
designed around a different prior: **multi-agent review only works if the
agents come from different model families.** Same-family coalitions don't
average bias away — they amplify it.

This document captures the competitive position and the specific shapes
that make mini-ork compose, not compete, with Claude Code, OpenAI Agents
SDK, LangGraph, and the new dynamic-workflow agents.

## The literature this rests on

| Paper | Finding |
|---|---|
| Nasser 2026 ([arxiv:2601.05114](https://arxiv.org/abs/2601.05114)) | 9-judge eval, 3240 ratings, Krippendorff α = 0.042. Claude-Opus harshness −0.429, Gemini-3-Pro +0.262. Same-family validators amplify disposition rather than average it. |
| Rajan 2025 ([arxiv:2511.16708](https://arxiv.org/abs/2511.16708)) | CodeX-Verify: multi-agent works submodularly **iff** pairwise ρ between voters is low (0.05–0.25). Heterogeneity isn't an optimization — it's the precondition for the proof. |
| Karanam 2025 ([arxiv:2512.21352](https://arxiv.org/abs/2512.21352)) | GPT-4o + Gemini 2.5 Pro + Grok 2 three-round vote: each persona catches a different ~88% of bugs, ~12% overlap. |
| Zietsman 2026 ([arxiv:2603.25773](https://arxiv.org/abs/2603.25773)) | AI-reviewing-AI without an executable specification is structurally circular. |
| Shehata 2026 ([arxiv:2604.27274](https://arxiv.org/abs/2604.27274)) | Consensus Paradox: homogeneous agents prioritise internal agreement over external truth. |
| Song 2026 ([arxiv:2603.21454](https://arxiv.org/abs/2603.21454)) | Repeating verification within one session **degrades** accuracy. Multi-turn review creates false positives faster than it catches errors. |

If you read one, read Nasser. The harshness table is the receipts.

## The detection fingerprint

> "List the model families behind every hunter and every validator. If the
> list reads 'Sonnet, Sonnet, Sonnet, Sonnet, Opus' you have an evaluative
> coalition, not an audit." — [sourceshift.io](https://blog.sourceshift.io/p/we-ran-a-3-source-bug-hunt-then-we-realised-our-validators-were-all-claude)

This is the test we built mini-ork to pass.

## How mini-ork wins over Claude Code dynamic workflows

Claude Code's new dynamic-workflow agents are a remarkable engineering
achievement at the per-session level: a model that decomposes its own task,
spawns sub-agents, integrates results, and writes a final report. We built
mini-ork on top of Claude Code (it's how researchers/reviewers actually
dispatch), so this section is not "vs" — it's "what does mini-ork add."

| Axis | Claude Code dynamic workflow | mini-ork |
|---|---|---|
| **Agent diversity** | All sub-agents are Anthropic-family (Sonnet/Opus). Coalition by construction. | `config/agents.yaml` maps lanes to GLM / Kimi / Codex / Opus / DeepSeek / MiniMax. Heterogeneity by configuration. |
| **State persistence** | Per-session. Ephemeral. The next session has no memory of the last one's findings. | `state.db` (SQLite) persists `task_runs`, `execution_traces`, `gradient_records`, `pattern_records`, `workflow_candidates`, `benchmark_results`, `version_registry`, `promotion_records` across sessions. |
| **Trajectory measurement** | None. Each session is a black box. | `mini-ork metrics --recipe X --since EPOCH` queries the substrate and emits cross-cycle trajectory: cost trend, wall-time trend, finding-discovery rate, gradient yield. |
| **Executable specification** | The model decides what counts as "good." | Recipes ship `verifiers/*.sh` deterministic gates. `artifact_contract.yaml` declares `success_verifiers` + `outputs[]`. The verifier IS the executable spec. |
| **Self-publishing** | Output stays in the session log. | Publisher node copies `synthesis.md` to canonical repo path + `git commit` under a `mini-ork@local` identity. The framework ships its own findings to durable storage. |
| **Cross-cycle improvement** | Each session starts from zero. | Reflect → improve → eval → promote chain reads `execution_traces` history, extracts gradients via LLM, proposes `workflow_candidates`, benchmarks them, and promotes via `version_registry`. |
| **Process model** | Single-process orchestration via tool-use protocol. | Per-node OS-process dispatch via bash subshells + claude/codex/cl_glm wrappers. Each agent gets a clean process, isolated env, real timeouts. |
| **Reproducibility** | One run per prompt. | `mini-ork run <recipe> <kickoff>` is deterministic given the same kickoff. State.db captures full run lineage. |

The compose model is correct: Claude Code is the engine. mini-ork is the
operating system that schedules engines from multiple families, persists
their work, measures trajectory, and proves improvement.

## The five claims you can verify

### 1. Heterogeneous-family by design

```yaml
# config/agents.yaml — lane assignment
lanes:
  researcher: sonnet
  reviewer: opus               # cross-family arbiter
  decomposer: deepseek         # different family
  # recipe-specific lanes:
  glm_lens: glm                # Zhipu
  kimi_lens: kimi              # Moonshot
  codex_lens: codex            # OpenAI Codex
  opus_lens: opus              # Anthropic
```

`recipes/refactor-audit/workflow.yaml` dispatches 4 named lenses to 4 distinct
families. Pairwise ρ (per Rajan 2025) is low by construction.

Provider wrappers ship at `lib/providers/cl_{glm,kimi,codex,deepseek,opus,sonnet,minimax}.sh`
— 7 model-family routes available out-of-the-box.

### 2. Persistent learning substrate

```bash
mini-ork metrics --recipe refactor-audit | head
# # mini-ork trajectory
# **Cycles:** 12
# **Total cost:** $17.10
# **Total traces:** 6+ per recent cycle
# **Total gradients:** N (extracted via reflect)
```

`state.db` is the substrate. The framework can answer: "what did the audit
cycle from 3 days ago find that today's didn't?" — Claude Code cannot.

### 3. Executable specification gate

```bash
# Every recipe ships verifiers/*.sh
recipes/refactor-audit/
  artifact_contract.yaml      # outputs + success_verifiers
  verifiers/lens-completeness.sh  # deterministic check
```

`mini-ork-verify` runs the recipe's verifier scripts. Pass/fail is mechanical,
not LLM-judged. Closes Zietsman 2026's structural-circularity gap.

### 4. Self-publishing under mini-ork@local identity

```
$ git log --author="mini-ork" --oneline
e96b5cb audit(refactor-audit): publish synthesis from run-1780298691-99474
43ed037 audit(refactor-audit): publish synthesis from run-1780241430-30697
cf33521 audit(refactor-audit): publish synthesis from run-1780239183-75632
```

Three real auto-commits, real synthesis content at `docs/refactor/synthesis-latest.md`.
The framework ships its own findings.

### 5. Cross-DF metric trajectory

```bash
mini-ork metrics --recipe refactor-audit --format json | jq '.totals'
# {"cycle_count": 12, "total_cost_usd": 17.10, "trace_count": 6, "gradient_count": N}
```

Phase C scaffold. Cross-cycle delta auto-detect coming in v0.3.

## Where mini-ork is honest about what it isn't (yet)

- **Krippendorff α calibration gate** — not built. Reviewer pool currently
  doesn't compute α across deliberators' first-round proposals. v0.3
  candidate per Nasser 2026.
- **Adversarial fabricated-bug injection** — not built. v0.3 candidate per
  Agarwal 2026 *Refute-or-Promote* arxiv:2604.19049.
- **Wireheading check** — partially: rich trace_write captures `files_read`/
  `tool_calls` (D-042 fix), but the validator-actually-read-the-file gate
  isn't enforced yet.
- **Honest confidence intervals on every claim** — recipes don't yet emit
  "P1 ± 1 (95% CI [P0, P2]) per N=4 validators with κ=0.3". v0.3 candidate
  per Dai 2025 *Semantic Triangulation* arxiv:2511.12288.

These are explicitly on the roadmap (`ROADMAP.md`). The honesty matters:
mini-ork solves the **heterogeneity precondition** today; it's working toward
the calibration + adversarial-injection + CI gates that turn that precondition
into a robust process.

## The one-line summary

mini-ork is what you build on top of Claude Code (or any single-vendor agent
framework) when you've read Nasser 2026 and want to actually pass the
detection-fingerprint test.

---

*See also: [README.md](../../README.md) · [ROADMAP.md](../../ROADMAP.md) ·
[docs/refactor/SCALABILITY-AUDIT.md](../refactor/SCALABILITY-AUDIT.md) for the
22-commit dogfood arc where the framework audited itself with 4-family lenses.*
