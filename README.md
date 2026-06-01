# mini-ork

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.1-green.svg)](CHANGELOG.md)
[![CI](https://img.shields.io/badge/CI-shellcheck%20%2B%20smoke-yellow.svg)](.github/workflows/ci.yml)
[![Status](https://img.shields.io/badge/status-early%20preview-orange.svg)](ROADMAP.md)

mini-ork is a **task operating system for agents**. It receives a goal, classifies the work, chooses a workflow, dispatches specialized agents, verifies artifacts, and stores execution experience for self-improvement. It does NOT ship opinions on what your pipeline should look like — pipeline shapes live in [`recipes/`](./recipes/) as composable user-land examples.

> 🧭 **Why mini-ork vs Claude Code / OpenAI Agents SDK / LangGraph dynamic workflows:** see [`docs/positioning/why-mini-ork.md`](docs/positioning/why-mini-ork.md). TL;DR: most agent frameworks ship multi-agent review where **every agent is the same model family**. That's the [evaluative coalition bias](https://blog.sourceshift.io/p/we-ran-a-3-source-bug-hunt-then-we-realised-our-validators-were-all-claude) the literature ([Nasser 2026](https://arxiv.org/abs/2601.05114), [Rajan 2025](https://arxiv.org/abs/2511.16708)) flags as the failure mode. mini-ork dispatches lenses to **distinct families by configuration** (GLM, Kimi, Codex, Opus, DeepSeek, MiniMax) — the heterogeneity precondition for Rajan's submodularity proof, met by construction.

> ⚡ **60-second demo (no API keys):** `bash examples/00-demo.sh` — bootstraps a throwaway project, runs the loop in dry-run mode, prints the `task_runs` row.

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

# 4. Run a recipe (dry-run first, no API keys needed)
MINI_ORK_DRY_RUN=1 mini-ork run code-fix ./kickoff.md

# 5. For real LLM calls (needs `claude` CLI authenticated)
mini-ork run code-fix ./kickoff.md
```

`mini-ork run` exits 0 on verified artifact, 1 on gate failure or escalation. All state is in `${MINI_ORK_DB}` (default: `.mini-ork/state.db`). Inspect with:

```bash
sqlite3 .mini-ork/state.db "SELECT id, task_class, recipe, status, verdict FROM task_runs ORDER BY created_at DESC LIMIT 5;"
```

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
| 8 node-type interfaces | `lib/agent_registry.sh` | planner / researcher / implementer / reviewer / verifier / reflector / publisher / rollback |
| 6 edge-type semantics | `schemas/workflow.schema.json` | depends_on / supplies_context_to / verifies / blocks / retries / escalates_to |
| 6 gate types | `lib/gate_registry.sh` | deterministic / reviewer / human / budget / scope / deployment |
| 8 memory namespaces | `db/migrations/` | task / workflow / agent_performance / failure / recovery / user_preference / artifact / benchmark |
| Task-class registry | `${MINI_ORK_HOME}/config/task_classes/*.yaml` | typed task definitions |
| Workflow registry | `recipes/<recipe>/workflow.yaml` | versioned DAGs |
| Benchmark suite | `lib/benchmark_suite.sh` | eval harness |
| Promotion gate | `lib/promotion_gate.sh` | utility_delta + benchmark gate |
| Version registry | `lib/version_registry.sh` | promote / quarantine / rollback |
| Group evolver | `lib/group_evolver.sh` | workflow candidate generation |
| Experience memory | `lib/trace_store.sh` + `lib/gradient_extractor.sh` + `lib/pattern_store.sh` | store, extract, surface |

### RECIPES — opinions live here

Recipes are user-land workflow definitions. They compose framework primitives into pipeline shapes.

| Recipe | Location | Shape |
|---|---|---|
| `code-fix` | `recipes/code-fix/` | minimal reference: classify → plan → implement → verify |
| `bdd-first-delivery` | `recipes/bdd-first-delivery/` | full: decompose → workers → BDD spec → review → merge |

Add your own under `recipes/<name>/` — see [docs/EXTENSION.md](docs/EXTENSION.md).

---

## 4 Extension Points

Extensions do not require forking the framework. See [docs/EXTENSION.md](docs/EXTENSION.md) for full examples.

1. **WorkflowGraph** — add nodes and edges by writing a `workflow.yaml` in your recipe. Validated against `schemas/workflow.schema.json`.
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

### v0.1 (current — this release)

- Universal 6-stage loop (`classify → plan → execute → verify → reflect → improve`)
- 13 framework primitives in `lib/`
- 8 `bin/` entrypoints
- 4 new schemas + 4 new migrations (memory namespaces, benchmarks, evolution, safety)
- `recipes/code-fix/` reference recipe
- `recipes/bdd-first-delivery/` ported from v0.0 BDD pipeline as a recipe

### v0.2

- `mini-ork resume <run-id>` — continue interrupted loop from last checkpoint
- `mini-ork inspect <task-id>` — trace viewer (iter log, model costs, gradient log)
- Per-recipe benchmark fixtures
- Speculative dispatch mode (multiple workflow candidates compete before selection)
- Parallel lane cap + scope-conflict detector

### v1.0

- Web dashboard (read-only, sqlite-backed)
- `mini-ork replay <task-id>` — re-run a specific task against current HEAD
- Plugin hooks system (pre-node, post-node, on-gate-fail)
- Cost budget enforcement (`--budget 5.00`)
- Built-in recipes: `research-synthesis`, `blog-post`, `ui-audit`, `db-migration`, `ops-runbook`

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

All deps invoked as external processes — nothing is bundled. Run `bash install.sh --check` to verify deps before first use.

---

## License

Apache-2.0. See [LICENSE](LICENSE).
