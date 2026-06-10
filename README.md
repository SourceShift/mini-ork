# mini-ork

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/SourceShift/mini-ork?label=release&color=green)](https://github.com/SourceShift/mini-ork/releases/latest)
[![CI](https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml)
[![Status](https://img.shields.io/badge/status-early%20preview-orange.svg)](ROADMAP.md)

![A friendly modern red master orc coordinates peaceful mini-ork bubble worlds inside a spaceship, with tiny green mini-orc agents collaborating through connected workspaces.](assets/mini-ork-hero.jpg)

> **Motto:** Stop drawing agent graphs that let one model family grade its own homework. mini-ork turns goals into verifier-gated, stateful, cross-family agent runs, so teams get durable artifacts instead of same-vendor consensus theater.

mini-ork is a **task operating system for agents**. It receives a goal, classifies the work, chooses a workflow, dispatches specialized agents, verifies artifacts, and stores execution experience for self-improvement. It does NOT ship opinions on what your pipeline should look like — pipeline shapes live in [`recipes/`](./recipes/) as composable user-land examples.

> ⚡ **10-second demo (no API keys):** `bash examples/00-demo.sh` — bootstraps a throwaway project, walks the classify → plan → execute → verify loop in dry-run mode (no LLM calls), prints the dispatched node sequence + the plan path that *would* be written. Set `MINI_ORK_DRY_RUN=0` to fire real LLM calls and populate the `task_runs` row.

---

## Why heterogeneous-family multi-agent (the load-bearing claim)

**Most agent frameworks ship multi-agent review where every agent is the same model family.** That's the [evaluative coalition](https://blog.sourceshift.io/p/we-ran-a-3-source-bug-hunt-then-we-realised-our-validators-were-all-claude) failure mode the literature has now named, measured, and assigned harshness coefficients to. A panel of four Sonnets isn't four independent judges — it's one disposition amplified four times.

mini-ork is built around the opposite prior: **dispatch lenses to distinct model families by configuration.** The literature below does not prove that vendor diversity alone is sufficient; it supports a narrower, more useful design rule: multi-agent review needs low-correlation evidence channels, executable checks, and information boundaries. mini-ork uses model-family diversity as an enforceable proxy for that independence, then adds deterministic verifiers where possible.

### Research signals behind the design

| Paper | What it supports |
|---|---|
| [Nasser 2026](https://arxiv.org/abs/2601.05114) — *Evaluative Fingerprints* | 9-judge eval, 3240 ratings: Krippendorff α = **0.042**. Claude-Opus harshness −0.429, Gemini-3-Pro +0.262. LLM judges are stable measurement instruments with different dispositions, not interchangeable graders. |
| [Rajan 2025](https://arxiv.org/abs/2511.16708) — *Multi-Agent Code Verification via Information Theory* | CodeX-Verify argues that specialized detectors help when detection patterns are conditionally independent. It reports agent correlations ρ = 0.05-0.25 and diminishing gains across 1-4 agents. mini-ork treats low ρ as the target and model-family diversity as an operational proxy. |
| [Karanam 2025](https://arxiv.org/abs/2512.21352) — *Multi-Agent LLM Committees for Autonomous Software Beta Testing* | A GPT-4o + Gemini 2.5 Pro + Grok 2 Vision committee improves beta-testing task success and bug-detection F1 over single-agent baselines. Persona-diversity analysis reports that only roughly 12% of bugs are found by more than one persona. |
| [Zietsman 2026](https://arxiv.org/abs/2603.25773) — *Specification as Quality Gate* | Argues that AI-reviewing-AI is structurally circular without executable specifications. This supports mini-ork's verifier-first design: model review is residual judgment, not the oracle. |
| [Shehata 2026](https://arxiv.org/abs/2604.27274) — *Inverse-Wisdom Law* | Reports a "Consensus Paradox" where kinship-dominant swarms can converge on internal agreement instead of external truth. Treat as a warning signal for same-family panels, not a settled universal law. |
| [Song 2026](https://arxiv.org/abs/2603.21454) — *Cross-Context Verification* | Supports session isolation and information restriction. The paper's own pilot and cited related work show repeated/shared-context verification can create sycophantic confirmation and false-positive pressure. |

### The detection-fingerprint test

> "List the model families behind every hunter and every validator. If the list reads 'Sonnet, Sonnet, Sonnet, Sonnet, Opus' you have an evaluative coalition, not an audit."

Run this test on any agent framework you're evaluating. mini-ork passes by construction:

```yaml
# config/agents.yaml — recipe-level lane assignment
lanes:
  # 4-family heterogeneous audit lenses
  glm_lens:     glm         # Zhipu
  kimi_lens:    kimi        # Moonshot
  codex_lens:   codex       # OpenAI Codex
  opus_lens:    opus        # Anthropic Opus
  minimax_lens: minimax     # MiniMax (M3, opt-in 5th lens where budget allows)
  # cross-family synthesizer / reviewer lane
  reviewer:     opus        # Anthropic
  decomposer:   deepseek    # DeepSeek (different family for planning)
```

7 model-family wrappers ship out of the box at [`lib/providers/`](lib/providers/): `cl_{glm,kimi,codex,deepseek,opus,sonnet,minimax}.sh`. The audit recipe at [`recipes/refactor-audit/`](recipes/refactor-audit/) uses all 4 distinct lens families per cycle (glm + kimi + codex + opus). MiniMax is available as an opt-in additional family for recipes that can afford a 5th lens.

### What you trade for what

| You give up | You get |
|---|---|
| The convenience of one vendor's billing | Cross-family bias diversity (Nasser 2026) |
| Same-vendor caching tricks | Lower-correlation review lanes inspired by Rajan 2025 |
| Single-vendor SLA | Independence of failure modes — one vendor outage doesn't kill the cycle |
| Uniform model behavior | Persona-differentiated bug catches (Karanam 2025) |

### How it compares to Claude Code / OpenAI Agents SDK / LangGraph dynamic workflows

| Axis | Single-vendor agent SDKs | mini-ork |
|---|---|---|
| Agent diversity | All same family (Sonnet/Opus, or GPT-4/o1, etc) | 7 families configurable per lane |
| State persistence | Per-session, ephemeral | `state.db` (SQLite) across runs |
| Trajectory measurement | None | `mini-ork metrics` cross-cycle |
| Executable specification | Model decides what's good | `verifiers/*.sh` deterministic gates |
| Self-publishing | Output stays in session log | Publisher node `git commit` under `mini-ork@local` |
| Cross-cycle improvement | Each session starts from zero | reflect → improve → eval → promote chain |
| Reproducibility | Run-to-run drift | Deterministic given same kickoff |

**Composition, not competition:** mini-ork dispatches Claude Code, codex, gemini-cli, GLM, Kimi etc as worker agents. The framework is the operating system; the vendor SDKs are the engines.

📖 **Deeper writeup:** [`docs/positioning/why-mini-ork.md`](docs/positioning/why-mini-ork.md) — 6-paper lit review + 5 verifiable claims + honest "what we haven't built yet" section.

---

## Quickstart

```bash
# 1. Install (creates symlink in $HOME/.local/bin or /usr/local/bin)
bash install.sh

# 2. Initialize a project (creates .mini-ork/ + seeds state.db + task_classes)
cd ~/my-project
mini-ork init

# 3. Write a kickoff (or copy an example)
cp ~/ps/mini-ork/examples/01-hello-world/kickoff.md ./kickoff.md

# 4. Run from a kickoff (dry-run first, no API keys needed)
MINI_ORK_DRY_RUN=1 mini-ork run ./kickoff.md

# Or force a recipe explicitly
MINI_ORK_DRY_RUN=1 mini-ork run code-fix ./kickoff.md

# 5. For real LLM calls (needs `claude` CLI authenticated)
mini-ork run ./kickoff.md
```

`mini-ork run` exits 0 on verified artifact, 1 on gate failure or escalation. All state is in `${MINI_ORK_DB}` (default: `.mini-ork/state.db`). Inspect with:

```bash
sqlite3 .mini-ork/state.db "SELECT id, task_class, recipe, status, verdict FROM task_runs ORDER BY created_at DESC LIMIT 5;"
```

### Observability UI (optional)

A read-only React SPA backed by FastAPI exposes the same `state.db` + run
artifacts as a browseable surface — fleet view, per-run DAG forensics,
trajectory metrics, and the **detection-fingerprint** panel that audits
which model families ran which lens per recipe.

```bash
# 1. Install backend deps (one-time)
pip install fastapi uvicorn pyyaml

# 2. Boot the local UI (binds 127.0.0.1:7090, read-only)
mini-ork serve

# 3. Browse to http://127.0.0.1:7090
```

The SPA bundle ships under `mini_ork/web/static/` after `pnpm --dir ui build`.
For dev with hot reload, run `pnpm --dir ui dev` (Vite on :5173 proxies to :7090).
Routes: `/` fleet, `/runs/:id` forensics, `/trajectory` convergence, `/fingerprint` coalition audit.

---

## Architecture

```
kickoff.md
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│  classify                                                        │
│  task_class + risk + artifact_contract + verifier_contract       │
└───────────────────────┬──────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│  plan                                                            │
│  objective · decomposition · dependencies · risk · verifier def  │
└───────────────────────┬──────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│  execute   (workflow.yaml DAG — dispatched per recipe)           │
│                                                                  │
│  ┌──────────┐  ┌────────────┐  ┌──────────────┐  ┌──────────┐  │
│  │ planner  │→ │ researcher │→ │ implementer  │→ │ reviewer │  │
│  └──────────┘  └────────────┘  └──────────────┘  └────┬─────┘  │
│                                                        │        │
│  ┌────────────┐  ┌───────────┐  ┌───────────┐         │        │
│  │ reflector  │  │ publisher │  │ rollback  │ ←────── │        │
│  └────────────┘  └───────────┘  └───────────┘         │        │
│                                              ┌─────────┘        │
│                                              ▼                   │
│                                       ┌──────────┐              │
│                                       │ verifier │              │
│                                       └──────────┘              │
└───────────────────────┬──────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│  verify   (gates — deterministic / reviewer / human / budget)    │
└───────────────────────┬──────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│  reflect                                                         │
│  ExecutionTrace → TextualGradient → PatternRecord                │
└───────────────────────┬──────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│  improve                                                         │
│  WorkflowCandidates → BenchmarkSuite → PromotionGate             │
│  → VersionRegistry (with rollback pointer)                       │
└──────────────────────────────────────────────────────────────────┘
```

---

## What ships in the framework vs. what lives in recipes

### FRAMEWORK — zero opinions on pipeline shape

The framework ships the universal loop and its primitives. Nothing in `lib/` or `bin/` knows about your domain.

| Primitive | Location | Purpose |
|---|---|---|
| Universal loop | `bin/mini-ork-{classify,plan,execute,verify,reflect,improve}` | 6-stage lifecycle |
| 8 node-type interfaces | `schemas/workflow.schema.json` + `bin/mini-ork-execute` | planner / researcher / implementer / reviewer / verifier / reflector / publisher / rollback |
| Agent version registry | `lib/agent_registry.sh` | per-role agent versions (model, provider, tools, success_rate, known_failure_modes) |
| 6 edge-type semantics | `schemas/workflow.schema.json` | depends_on / supplies_context_to / verifies / blocks / retries / escalates_to |
| 7 built-in gate types + `custom` | `lib/gate_registry.sh` | deterministic_verifier / reviewer_gate / human_gate / budget_gate / scope_gate / deployment_gate / liveness_gate + `custom` escape hatch |
| Behavioral circuit breaker | `lib/circuit_breaker.sh` | three orthogonal stagnation signals (artifact-hash invariance / verdict-stuck / cost-burn-without-write) with CLOSED→OPEN→HALF_OPEN state machine. Behavioral complement to the `MO_DAILY_BUDGET_USD` cost-CB (v0.2 Phase D) |
| 8 memory namespaces | `db/migrations/` | task / workflow / agent_performance / failure / recovery / user_preference / artifact / benchmark |
| Task-class registry | `${MINI_ORK_HOME}/config/task_classes/*.yaml` | typed task definitions |
| Workflow registry | `recipes/<recipe>/workflow.yaml` | versioned DAGs |
| Benchmark suite | `lib/benchmark_suite.sh` | eval harness |
| Promotion gate | `lib/promotion_gate.sh` | utility_delta + benchmark gate |
| Version registry | `lib/version_registry.sh` | promote / quarantine / rollback |
| Group evolver | `lib/group_evolver.sh` | workflow candidate generation |
| Experience memory | `lib/trace_store.sh` + `lib/gradient_extractor.sh` + `lib/pattern_store.sh` | store, extract, surface |
| Recursive orchestration | `bin/mini-ork-spawn` + `lib/recursive_orchestration.sh` | bounded parent/child mini-ork delegation with lineage, events, and policy limits |

### RECIPES — opinions live here

Recipes are user-land workflow definitions. They compose framework primitives into pipeline shapes. 11 recipes ship today; 7 of them dispatch a 4–5 lens panel across distinct model families per cycle, using family diversity as a practical proxy for the low-correlation detector patterns highlighted by Rajan 2025.

| Recipe | Location | Shape |
|---|---|---|
| `code-fix` | `recipes/code-fix/` | Single-patch fix with typecheck, test, and reviewer gates. Minimal reference recipe. |
| `bdd-first-delivery` | `recipes/bdd-first-delivery/` | BDD-first multi-epic delivery: decompose → parallel (spec_author + implementer) → bdd_runner → reviewer → publisher. |
| `docs` | `recipes/docs/` | Single-doc edit verified by grep-pattern assertions + relative-link integrity. No typecheck / test / rollback (docs edits are reversed via `git restore`). |
| `refactor-audit` | `recipes/refactor-audit/` | 4 lens stances run in parallel (glm/kimi/codex/opus), with Opus preserved as the architectural-shape lens. The framework's own self-audit recipe. |
| `research-synthesis` | `recipes/research-synthesis/` | 4-lens research synthesis (web/lit/code/narrative on distinct families) → synthesizer → publisher. |
| `post-mvp-delivery` | `recipes/post-mvp-delivery/` | Discovery-first post-MVP product delivery: parallel product/architecture/integration/validation research → options for user choice → selected-option gate → implementation. |
| `recursive-self-improve` | `recipes/recursive-self-improve/` | Wall-clock-budgeted self-improvement loop for mini-ork itself: bottleneck scan + heterogeneous lenses + arXiv evidence lane + synthesis + gated patch. Outer driver: `bin/mini-ork-self-improve`. |
| `blog-post` | `recipes/blog-post/` | 5-lens blog drafting (editor / researcher / narrative / audience / counter) in parallel across distinct families. |
| `db-migration` | `recipes/db-migration/` | 5-lens migration audit + plan: integrity / rollback / perf / compat / edge-data in parallel across distinct families. |
| `ops-runbook` | `recipes/ops-runbook/` | 5-lens runbook generation: detection / containment / diagnosis / recovery / prevention across distinct families. |
| `ui-audit` | `recipes/ui-audit/` | 5-lens UI audit: a11y / perf / visual / interaction / edge-cases across distinct families. |

Add your own under `recipes/<name>/` — see [docs/EXTENSION.md](docs/EXTENSION.md).

---

## 4 Extension Points

Extensions do not require forking the framework. See [docs/EXTENSION.md](docs/EXTENSION.md) for full examples.

1. **WorkflowGraph** — add nodes and edges by writing a `workflow.yaml` in your recipe. The live recipes are the executable contract today; `schemas/workflow.schema.json` is the target validation contract and is being aligned with the newer recipe fields such as verifier refs and human decision edges.
2. **AgentRegistry** — register new roles or model bindings via `lib/agent_registry.sh:agent_register`. No code change.
3. **VerifierRegistry** — drop a `<name>.sh` script under `${MINI_ORK_HOME}/verifiers/` or `recipes/<recipe>/verifiers/` and reference it in `workflow.yaml`.
4. **ExperienceMemory** — add new namespaces via DB migrations or override `lib/context_assembler.sh` per task class.

---

## Bounded Autonomy

Self-improvement is evidence-gated, not free-running. Changes are ranked by risk:

| Rung | Mutation | Gate required |
|---|---|---|
| 1 | Tune prompt wording | None — always safe |
| 2 | Tune retrieval / context assembly | None — always safe |
| 3 | Tune workflow graph edges | Benchmark pass |
| 4 | Tune agent role definitions | Benchmark pass |
| 5 | Tune verifier selection | Benchmark pass |
| 6 | Propose code changes to mini-ork itself | Benchmark pass + human review |
| 7 | Promote runtime changes | Benchmark pass + human gate + `version_clear_quarantine` if previously quarantined |

See [docs/SAFETY.md](docs/SAFETY.md) for immutable constraints and the PromotionGate contract.

---

## Roadmap

**Current: v0.3.0-rc1** (release candidate, 2026-06-08) — oracle-hardening primitives shipped: `coalition_gate.sh`, `cw_por.sh`, `mo_promote_synthesis_gate`, `adaptive_stability.sh`, `circuit_breaker.sh`, plus the central `gate_bootstrap.sh` wiring used by execute. Self-evolution is now explicitly class-restricted (`docs/positioning/why-mini-ork.md` §"Self-evolution is class-restricted").

The full release log lives in [`ROADMAP.md`](ROADMAP.md) — every section dated and per-commit-attributed. Current shipped totals (regenerable via `bash scripts/readme-claim-check.sh` and filesystem counts):

- 6-stage universal loop (`classify → plan → execute → verify → reflect → improve`) + 7 companion entrypoints (`eval`, `improve`, `promote`, `metrics`, `spawn`, direct `bin/mini-ork-topology`, direct `bin/mini-ork-self-improve`)
- 43 framework primitives in `lib/` (incl. oracle-hardening libs + `gate_bootstrap.sh` for the v0.3-rc1 central wire-up + `lib/throttle-guard.sh` for provider-throttle classification with per-lane exponential backoff, added 2026-06-09)
- 1 runner-shared helper in `bin/lib/` (`profile-seed.sh` — deterministic `run_profile.json` seeding from structured kickoff markdown, added 2026-06-09)
- 15 user-facing `bin/mini-ork*` entrypoints
- 17 schema migrations under `db/migrations/` (memory namespaces, benchmarks, evolution, safety, panel topology telemetry, recursive orchestration, self-improvement learning)
- 11 recipes shipped — see Recipes table above
- 7 model-family providers under `lib/providers/`

Next-up work tracks (see [`ROADMAP.md`](ROADMAP.md) for detail):

- Align JSON schemas, documentation examples, and the live recipe YAML dialect so extension authors get one authoritative contract
- Wave 2-A held-out anchor corpus per synthesis recipe (Wang 2026)
- Wave 3 mechanical citation+coverage verifier (Sistla 2025 + Ficek 2025)
- Krippendorff α calibration gate + adversarial fabricated-bug injection (the v0.2 honest-gaps list)

---

## Recursive self-improvement evidence (2026-06-09 session)

The `recursive-self-improve` recipe ran against mini-ork itself for ~5 wall-clock hours, producing **10 commits to `main` autonomously** — each grounded in cited arXiv evidence per the recipe's "new infra requires arXiv evidence" hard rule. Audit trail lives in `self_improve_runs`, `learning_record`, and `self_improve_arxiv_refs` tables; per-iter synthesis files are preserved under `.mini-ork/runs/`.

| Iter | Commit | Technique | arXiv citation(s) |
|---|---|---|---|
| 1  | `c5b819c` | Verifier verdict JSON adapter (`_run_verifier_ref`) | — (in-place adapter, no infra) |
| 2  | `e95e641` | Broader pollution check across all `lens-*.md` artifacts | 2602.13477 Naik 2026; 2502.12630 Sternak 2025 |
| 3  | `6a66e28` | Post-write envelope sanitizer at consumer boundary | 2604.01350 Yang 2026; 2605.16746 Wang 2026 |
| 4  | `94b48c8` | Optional `lens-arxiv.md` when provider capacity errors | — (operational) |
| 18 | `f8967b1` | Utility-delta tri-state gate in `no-regression.sh` | 2604.10547 Chen 2026; 2604.00072 Scrivens 2026 |
| 19 | `0a3bf1c` | Pre-dispatch profile gate in `bin/mini-ork-plan` | 2605.07062 Barnes 2026 |
| 20 | `300fe48` | Canonical worktree base-ref resolution | 2603.25697; 2604.07877; 2511.06179 |
| 32 | `b9b6d18` | Portable `duration_ms` capture at `llm_dispatch` sites | 2604.05119 Pathak 2026; 2601.08815 Ye 2026; 2604.23853 Yuan 2026; 2602.10133; 2602.19065; 2605.27328 |
| 33 | `77f965f` | Deterministic profile-seed from structured kickoff markdown | 2601.04620; 2604.08633 |

Cumulative DB state at session end: **23 arXiv references cited across the session; 19 marked `used_in_patch=1`** (i.e. landed in main via cherry-pick). The recipe's safety rule was respected in both directions — every patch proposing new infrastructure cited a paper; every in-place adapter explicitly stated no citation required.

The first three patches (`c5b819c`, `e95e641`, `6a66e28`) were emergent — the loop found these bugs by reading its own prior run logs. Iter 18 (utility-delta) and iter 32 (duration_ms) targeted operator-seeded `learning_record` rows. Iter 33 is the most interesting: it healed a symptom of its own iter-19 patch (the profile-gate caused a spiral under a meta-kickoff; iter 33 fixed the root cause by deterministically populating the profile from structured kickoff sections, making the gate's "needs_answers" verdict honest rather than blocking).

Supporting fixes that landed alongside the loop's autonomous output: `lib/throttle-guard.sh` (provider-error classification + per-lane backoff + systemic-halt at 3 simultaneous providers), `bin/mini-ork-self-improve` pre-iter cost-cap pre-check (halt before worktree creation when `SUM(task_runs.cost_usd)` over 24h exceeds `MO_DAILY_BUDGET_USD`), Anthropic-native wrapper policy clarification (`cl_opus.sh` / `cl_sonnet.sh` only unset env vars, deferring to Claude Code ambient auth — gateway wrappers `cl_glm.sh` / `cl_kimi.sh` / `cl_minimax.sh` / `cl_deepseek.sh` keep setting `ANTHROPIC_AUTH_TOKEN` because they route to non-Anthropic endpoints).

Operational env vars added during this session: `MO_DAILY_BUDGET_USD` (cost circuit cap), `MINI_ORK_PROFILE_GATE` (planner profile gate; off by default for the recursive loop), `MINI_ORK_PLAN_CONFIDENCE_FLOOR` (gate threshold), `MINI_ORK_SELF_IMPROVE_BASE_REF` (worktree base ref, defaults to `main`), `MINI_ORK_BENCH_UTILITY_THRESHOLD` + `MINI_ORK_BENCH_MIN_N` (utility-delta gate parameters), `MINI_ORK_THROTTLE_EMPTY_ITER_THRESHOLD` (spiral halt), `MINI_ORK_PRE_ITER_COST_CHECK` (pre-iter cost-cap pre-check override). See `docs/RECURSIVE-SELF-IMPROVE.md` for the operator guide.

---

## Dependencies

| Dep | Version | Purpose |
|---|---|---|
| bash | 4.0+ | arrays, `mapfile`, `[[ ]]`, process substitution |
| sqlite3 | 3.35+ | state.db — WAL mode required |
| jq | 1.6+ | JSON parsing for LLM responses and schema validation |
| yq | 4.0+ | YAML config parsing (`task_classes/*.yaml`, `workflow.yaml`) |
| git | 2.28+ | worktrees, merge, rebase, branch quarantine |
| claude CLI | 2.1+ | `claude --print` subprocess agents |

All deps invoked as external processes — nothing is bundled. Run `mini-ork doctor` after installation to verify every dep is present + reachable.

---

## License

Apache-2.0. See [LICENSE](LICENSE).
