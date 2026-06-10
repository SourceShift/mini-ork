# Why mini-ork — heterogeneous-family multi-agent, by construction

Most agentic frameworks ship multi-agent review and call it a day. mini-ork is
designed around a narrower prior: **multi-agent review needs low-correlation
evidence channels, executable checks, and information boundaries.** Same-family
coalitions are a common way to lose that independence. mini-ork therefore uses
model-family diversity as an enforceable proxy, then relies on deterministic
verifiers where the task allows it.

This document captures the competitive position and the specific shapes
that make mini-ork compose, not compete, with Claude Code, OpenAI Agents
SDK, LangGraph, and the new dynamic-workflow agents.

## Research signals behind the design

| Paper | Finding and design implication |
|---|---|
| Nasser 2026 ([arxiv:2601.05114](https://arxiv.org/abs/2601.05114)) | 9-judge eval, 3240 ratings, Krippendorff α = 0.042. Claude-Opus harshness −0.429, Gemini-3-Pro +0.262. LLM judges are stable instruments with distinct evaluative dispositions, so judge choice is a methodological decision. |
| Rajan 2025 ([arxiv:2511.16708](https://arxiv.org/abs/2511.16708)) | CodeX-Verify argues for specialized detectors with low redundancy: measured ρ = 0.05-0.25 and diminishing returns across 1-4 agents. mini-ork treats low correlation as the target; family diversity is the practical proxy it can enforce. |
| Karanam 2025 ([arxiv:2512.21352](https://arxiv.org/abs/2512.21352)) | GPT-4o + Gemini 2.5 Pro + Grok 2 Vision committees improve beta-testing task success and bug-detection F1 over single-agent baselines. Persona-diversity analysis reports only roughly 12% of bugs found by more than one persona. |
| Zietsman 2026 ([arxiv:2603.25773](https://arxiv.org/abs/2603.25773)) | Argues that AI-reviewing-AI without executable specifications is structurally circular. This supports verifier-first architecture and bounded model review. |
| Shehata 2026 ([arxiv:2604.27274](https://arxiv.org/abs/2604.27274)) | Reports the Consensus Paradox: kinship-dominant swarms can prioritize internal agreement over external truth. Useful warning signal for same-family panels, not a universal theorem. |
| Song 2026 ([arxiv:2603.21454](https://arxiv.org/abs/2603.21454)) | Supports session isolation and information restriction. Shared-context verifier chains can produce sycophantic confirmation; independent analytical contexts are the useful mechanism. |

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

## Self-evolution is class-restricted (don't oversell)

mini-ork's `promote` chain (reflect → improve → eval → promote via
`version_registry`) is a real durable substrate for cross-cycle learning.
But the *self-improving* claim has a class-restricted truth — and pretending
otherwise is the failure mode this section exists to prevent.

The framework supports two task-class families with **fundamentally
different promotion contracts**:

| Task class | Oracle | Auto-promote via `mini-ork promote`? |
|---|---|---|
| Deterministic | External ground truth (typecheck, test suite, schema validator, migration replay). The verifier returns rc=0/1; the oracle is not the framework. | **Yes.** `code_fix`, `db_migration` — auto-promote on green verifier. |
| LLM-judged only | No external oracle. The promotion gate is the same family distribution that produced the candidate. | **No — operator review required.** `research_synthesis`, `refactor_audit`, `blog-post`, `ui-audit`, `ops-runbook` are **manual-promote-only**: the operator reads the synthesis and decides. |

The reason for the split is mathematical, not stylistic:

- **Zenil 2026** ([arxiv:2601.05280](https://arxiv.org/abs/2601.05280))
  formally proves that recursive self-evolution without an external
  grounding signal `α_t` yields degenerative dynamics in the limit
  (entropy decay + distributional drift). For deterministic task classes
  the verifier *is* the `α_t`. For synthesis classes, no such signal
  exists in the loop — promotion driven by LLM rubric scoring is the
  closed system Zenil's theorem applies to.
- **Setlur 2025** ([arxiv:2502.12118](https://arxiv.org/abs/2502.12118),
  ICML, 82 citations) demonstrates empirically that test-time-compute
  scaling without external verification is suboptimal at every compute
  budget tested.
- **DeVilling 2025** ([arxiv:2510.21861](https://arxiv.org/abs/2510.21861))
  studies 144 recursive self-evaluation sequences and finds that "model
  reviewing its own output" is reformulation, not progress — no
  monotonic quality gain across iterations.

Synthesis-class recipes still get the full mini-ork substrate (state.db
trajectory, gradient extraction, candidate scoring, panel verdicts) — what
they don't get is auto-promotion. The operator stays in the loop because
the framework refuses to fabricate an oracle it doesn't have.

The v0.3 oracle-hardening epic ([`kickoffs/oracle-hardening-v03.md`](../../kickoffs/oracle-hardening-v03.md))
introduces three diagnostics that make this contract enforceable rather
than documentary: a ρ hard-block gate (Bertalanič 2026 family-diversity
precondition becomes load-bearing), a CW-POR authority-capture detector
(Agarwal & Khanna 2025), and a selective-feedback conjunction in
`promotion_gate.sh` (Adapala 2025 Anti-Ouroboros).

## Citation-honesty audit trail (2026-06-01)

The DF14 dogfood cycle of `research-synthesis` ran 4 distinct-family lenses
against this positioning doc's argument. **The synthesis flagged the
Rajan 2025 and Nasser 2026 citations as unverifiable by any lens** — all
4 lenses' web-search tools failed to surface the papers.

WebFetch verification against arxiv.org confirmed both citations are
**real and accurate**:

- [arxiv:2511.16708](https://arxiv.org/abs/2511.16708) "Multi-Agent Code Verification via Information Theory" by Shreshth Rajan — exact-match on title, submodularity claim, 4 specialists, ρ=0.05-0.25, 39.7-pp gain.
- [arxiv:2601.05114](https://arxiv.org/abs/2601.05114) "Evaluative Fingerprints" by Wajid Nasser — exact-match on Krippendorff α=0.042, evaluative-fingerprints concept, harshness/leniency axis.

The lens false-negatives are a known limitation: training-cutoffs and
web-search indexing predate Nov 2025+ papers. The lens prompts forbid
fabricating citations (must emit `[lookup: <query>]` placeholder), so the
panel honestly flagged the gap rather than hallucinate. External
WebFetch then closes the loop.

Full audit trail: [`docs/research/citation-verification-2026-06-01.md`](../research/citation-verification-2026-06-01.md)

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
