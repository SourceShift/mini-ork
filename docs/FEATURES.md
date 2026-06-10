# mini-ork Feature Catalog

The complete inventory of what mini-ork provides, organized by the value it
delivers. Counts are mechanically verified against the repo by
`scripts/readme-claim-check.sh`. Every feature below points at the code that
implements it.

mini-ork is a **task operating system for agents**: it receives a goal,
classifies the work, plans, dispatches specialized agents across *distinct
model families*, verifies artifacts deterministically, and stores execution
experience so the next run starts smarter than the last.

---

## 1. Heterogeneous multi-family orchestration

The load-bearing design choice: review panels span vendors, so no model
family grades its own homework.

| Feature | Where | What it gives you |
|---|---|---|
| Lane-based model routing | `.mini-ork/config/agents.yaml` + `lib/llm-dispatch.sh` | Recipes declare roles (`reviewer`, `glm_lens`); config maps roles to model families. Swap vendors without touching workflows. |
| 7 provider families out of the box | `lib/providers/cl_{glm,kimi,codex,deepseek,opus,sonnet,minimax}.sh` | Zhipu, Moonshot, OpenAI, DeepSeek, Anthropic ×2, MiniMax — each a clean process with isolated env and real timeouts. |
| BYO-key provider registry | `config/providers.yaml` + `lib/providers/registry.sh` | Declare any Anthropic/OpenAI-compatible endpoint (`anthropic-native`, `anthropic-compat`, `openai-compat`, `executable`) with your own API keys. No wrapper code needed. |
| Heterogeneous lens panels | `recipes/refactor-audit/`, `recipes/db-migration/`, others | 4–5 parallel review lenses on *distinct* families per cycle — low-correlation evidence channels, not same-vendor consensus. |
| Coalition gate (ρ hard-block) | `lib/coalition_gate.sh` | Pre-synthesis family-diversity check: blocks synthesis when the panel degenerates into a same-family coalition. |
| Panel topology telemetry | `lib/topology_metrics.sh` | Measures realised (ρ, C, I) per panel run; classifies panel health into 8 topology classes. |
| Persuasion-override diagnostic | `lib/cw_por.sh` | Detects authority capture — one agent steamrolling the panel. |
| Adaptive debate stability | `lib/adaptive_stability.sh` | Early-terminates multi-round panel debate once positions stabilize — saves rounds you'd otherwise pay for. |
| Detection-fingerprint audit | UI route `/fingerprint` | Shows which model families ran which lens per recipe — the "is this an audit or an echo chamber?" test, automated. |

## 2. Cost control — spend goes to a ledger, not a black hole

Single-agent coders bill you for re-reading your codebase every session and
route every token through one frontier-priced model. mini-ork's cost story
is structural:

| Feature | Where | What it gives you |
|---|---|---|
| Cheap-lane routing | `.mini-ork/config/agents.yaml` | Tactical checks run on budget families (GLM, DeepSeek); frontier models (Opus) are reserved for synthesis and architecture review. Price/role fit is configuration, not luck. |
| Per-call LLM ledger | `llm_calls` table (`db/migrations/0018_llm_calls_session_id.sql`) | Every dispatch logs model, tokens, cost, duration, session. Answer "where did the money go?" with SQL. |
| Authoritative usage totals | `lib/llm-dispatch.sh` | Token totals come from the billed result envelope, not stub per-turn values — costs you see are costs you paid. |
| Budget caps | `config/agents.yaml` (`budget:`) + `lib/llm-dispatch.sh` | Declared defaults: $5/epic, $0.50/run, $50/day. The daily cap is enforced inside the dispatcher (`MO_DAILY_BUDGET_USD`); per-lane budgets are surfaced to every dispatch as flags. |
| Daily cost circuit breaker | `MO_DAILY_BUDGET_USD` + pre-iter cost check in `bin/mini-ork-self-improve` | Long-running loops halt *before* creating the next worktree once 24h spend exceeds the cap. |
| Behavioral circuit breaker | `lib/circuit_breaker.sh` | Detects cost-burn-without-write, artifact-hash stagnation, and stuck verdicts — kills spinning runs instead of letting them bill. |
| Stage-level memoization | `lib/cache.sh` | LLM stages emit cache rows; identical stages reuse prior output instead of re-paying. |
| Rubric pre-screen | `lib/rubric-prescreen.sh` | Cheap 8-item context-grounded checklist runs *before* expensive test execution (arXiv 2601.04171). |
| Deterministic verifiers | `recipes/*/verifiers/*.sh` | Pass/fail is a shell script exit code — zero tokens spent on judging what a test suite can decide. |
| Escalate-up-only ladder | `config/agents/*.yaml` (`fallback_above`) + `escalates_to` edges in `bin/mini-ork-execute` | Work starts on the cheapest capable model. DAG `escalates_to` edges fire only on gate failure; agent definitions declare a `fallback_above` precision ladder terminating at opus (see `config/README.md`). |
| Throttle guard | `lib/throttle-guard.sh` | Classifies provider throttles, applies per-lane exponential backoff, halts systemically at 3 simultaneous provider failures — no retry storms. |
| Free dry-run mode | `MINI_ORK_DRY_RUN=1` | Full classify → plan → execute → verify walk with zero LLM calls. Debug pipelines for free. |
| Cheap observability smoke | `recipes/obs-smoke/` | 2-node recipe that exercises every telemetry surface for pennies. |

## 3. Experience memory — runs that learn

Each single-agent session starts from zero. mini-ork persists execution
experience and feeds it forward:

| Feature | Where | What it gives you |
|---|---|---|
| Persistent state substrate | `.mini-ork/state.db` (SQLite, WAL) | `task_runs`, `execution_traces`, `gradient_records`, `pattern_records`, `benchmark_results`, `version_registry` survive across sessions, branches, machines. |
| 8 memory namespaces | `db/migrations/` (19 migrations) | task / workflow / agent_performance / failure / recovery / user_preference / artifact / benchmark. |
| Execution traces with lineage | `lib/trace_store.sh` | Every node dispatch records tool calls, files read/written, cost, duration, workflow-version hash, and prompt-template hash — full provenance per artifact. |
| Textual gradient extraction | `lib/gradient_extractor.sh` + `bin/mini-ork-reflect` | Reflection turns failed/successful traces into natural-language "gradients": what to do differently next time, with confidence scores. |
| Prior-run memory injection | `lib/context_assembler.sh` + `bin/mini-ork-plan` | The planner prompt receives outcomes of the 5 most recent same-class runs (per-run: nodes, failures, cost, duration) — plans calibrate against history. |
| Learned-failure-mode injection | `bin/mini-ork-execute` | High-confidence gradients for the task class are injected into node prompts at dispatch time. |
| Auditable context packs | `bin/mini-ork-plan` (`context-pack.json`) | The full cite-tagged memory bundle available at plan time is persisted next to the plan — you can audit what the planner knew. |
| Pattern store | `lib/pattern_store.sh` | Recurring gradients consolidate into durable pattern records. |
| Agent performance history | `agent_performance_memory` + `lib/agent_registry.sh` | Success rate, cost, and latency accumulate per agent version — dispatch decisions get data. |
| Self-healing loop | `lib/healer.sh` | Reads a failed run's logs, classifies the failure, dispatches the matching recovery action. |
| Improvement chain | `bin/mini-ork-{reflect,improve,eval,promote}` + `lib/{group_evolver,benchmark_suite,promotion_gate,version_registry}.sh` | Gradients → workflow candidates → benchmark → gated promotion with rollback pointers. The pipeline itself evolves, with receipts. |
| Proof it works | README §"Recursive self-improvement evidence" | The loop ran against mini-ork itself: 10 autonomous commits to main in ~5h, each citing arXiv evidence, audit trail in `self_improve_runs`. |

## 4. Verification & safety — outcomes, not vibes

| Feature | Where | What it gives you |
|---|---|---|
| Deterministic verifier registry | `${MINI_ORK_HOME}/verifiers/` + `recipes/*/verifiers/` + `lib/gate_registry.sh` | Executable specs: exit 0 = pass. Recipe-scoped verifiers override globals. |
| 7 gate types + custom | `lib/gate_registry.sh` | deterministic_verifier / reviewer_gate / human_gate / budget_gate / scope_gate / deployment_gate / liveness_gate. |
| Minimum-evidence policy | `bin/mini-ork-verify` | Zero verifiers executed ⇒ verdict `vacuous`, *not* success. "Nothing was checked" is never laundered into a pass. |
| Run-scoped evidence | `bin/mini-ork-verify` | Verifier evidence files live with the run that produced them — every verdict is replayable. |
| Mutation adversary | `lib/mutation-adversary.sh` | Generates 5 plausible buggy diffs; the spec must catch ≥80% before implementation proceeds. Tests the tests. |
| Scope guards | `config/scope-patterns.yaml.example` + `lib/scope-overlap.sh` | Per-lane file-glob allow/deny — a backend worker can't quietly edit your frontend. |
| Bounded autonomy ladder | README §"Bounded Autonomy" + `docs/SAFETY.md` | 7 risk rungs from "tune prompt wording" (free) to "promote runtime changes" (benchmark + human gate + quarantine clear). |
| Class-restricted self-evolution | `docs/positioning/why-mini-ork.md` | Auto-promotion only for task classes with an external oracle (tests, schemas). LLM-judged classes stay operator-gated — the framework refuses to fabricate an oracle. |
| Branch quarantine + rollback | `lib/branch-quarantine.sh`, `lib/version_registry.sh`, rollback node type | Bad candidates are quarantined, promotions carry rollback pointers. |
| Worktree isolation | `lib/worktree-guard.sh` + `bin/mini-ork-spawn` | Risky work happens in disposable git worktrees, not your checkout. |
| Live steering | `lib/mo-steer.sh` | Push a steering message into a running worker mid-flight — course-correct without killing the run. |

## 5. Observability — watch the fleet, audit the run

| Feature | Where | What it gives you |
|---|---|---|
| Read-only web UI | `bin/mini-ork-serve` + `mini_ork/web/` + `ui/` | FastAPI + React over `state.db`: fleet view, per-run DAG forensics, agent transcripts, cost breakdowns. |
| Trajectory metrics | `bin/mini-ork-metrics` + UI `/trajectory` | Cross-cycle cost trend, wall-time trend, finding-discovery rate, gradient yield per recipe. |
| Coalition audit panel | UI `/fingerprint` | Which families ran which lens, per recipe, per run. |
| Run event stream | `lib/mo_node_events.sh` (`run_events`) | Node-level lifecycle events for live progress. |
| Honest status taxonomy | `db/migrations/0019_execution_traces_status_vacuous.sql` | `running` and `vacuous` are first-class statuses — in-flight work is visible, empty verification reads amber, not green. |
| OTel / Langfuse path | `docs/architecture/otel-langfuse.md` | Architecture for exporting traces to standard observability stacks. |

## 6. Extensibility — extend without forking

| Feature | Where | What it gives you |
|---|---|---|
| 4 canonical extension points | `docs/EXTENSION.md` | WorkflowGraph (YAML), AgentRegistry, VerifierRegistry, ExperienceMemory — all user-land. |
| Recipe system | `recipes/` (12 shipped) | Pipeline shapes are data, not framework code. Copy one, edit YAML, run. |
| Python facade | `mini_ork/` + `docs/PYTHON_FRAMEWORK.md` | Typed `MiniOrk().run(RunRequest(...))` embedding — host mini-ork inside your own app. |
| Recursive orchestration | `bin/mini-ork-spawn` + `lib/recursive_orchestration.sh` | Bounded parent/child mini-ork delegation with lineage tracking and policy limits. |
| Custom utility scoring | `${MINI_ORK_HOME}/config/utility_functions/` | Override the promotion utility function per task class. |
| Custom context assembly | `${MINI_ORK_HOME}/config/context_assemblers/` | Override memory retrieval per task class. |

---

## Inventory (mechanically checked)

### 16 user-facing entrypoints (`bin/`)

| Entrypoint | Role |
|---|---|
| `mini-ork` | Top-level runner: kickoff → recipe → full lifecycle |
| `mini-ork-init` | Project bootstrap: `.mini-ork/`, state.db, task classes |
| `mini-ork-classify` | Goal → task_class + risk + contracts |
| `mini-ork-plan` | Plan synthesis with memory injection + context pack |
| `mini-ork-execute` | Workflow DAG dispatch across lanes |
| `mini-ork-verify` | Deterministic verifier + gate execution |
| `mini-ork-reflect` | Trace → gradient extraction |
| `mini-ork-improve` | Workflow candidate generation |
| `mini-ork-eval` | Benchmark suite execution |
| `mini-ork-promote` | Gated promotion into version registry |
| `mini-ork-metrics` | Cross-cycle trajectory reporting |
| `mini-ork-spawn` | Bounded child-orchestrator delegation |
| `mini-ork-topology` | Panel topology measurement |
| `mini-ork-self-improve` | Wall-clock-budgeted self-improvement driver |
| `mini-ork-serve` | Observability UI server |
| `mini-ork-invoke-prompt` | Single-prompt lane dispatch utility |

### Substrate

- **44 framework primitives** in `lib/` — dispatch, gates, memory, evolution, safety, telemetry (full list: `ls lib/*.sh`)
- **19 schema migrations** in `db/migrations/` — the full memory + telemetry + safety substrate
- **12 recipes** in `recipes/` — see the README recipes table
- **7 provider families** in `lib/providers/` + BYO registry in `config/providers.yaml`

---

*Related: [README.md](../README.md) · [docs/EXTENSION.md](EXTENSION.md) ·
[docs/MODELS.md](MODELS.md) · [docs/SAFETY.md](SAFETY.md) ·
[docs/positioning/why-mini-ork.md](positioning/why-mini-ork.md)*
