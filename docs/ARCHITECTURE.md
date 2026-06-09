# mini-ork Architecture

mini-ork implements a universal task loop as a framework. Workflow shapes — the pipeline opinions — live in `recipes/` and are user-land. The framework ships only the loop, primitives, and interfaces.

---

## Universal Task Loop

```mermaid
flowchart LR
  classDef stage fill:#1f3a6e,stroke:#7aa6ff,stroke-width:2px,color:#fff
  classDef ok    fill:#1f4d1f,stroke:#6bd66b,stroke-width:2px,color:#fff
  classDef fail  fill:#5a1a1a,stroke:#ff6b6b,stroke-width:2px,color:#fff
  classDef warn  fill:#5a4416,stroke:#ffb84d,stroke-width:2px,color:#fff

  C[Classify]:::stage --> P[Plan]:::stage
  P --> E[Execute]:::stage
  E --> V[Verify]:::stage
  V -->|gates pass| R[Reflect]:::ok
  V -->|gate fail + retries remain| E
  V -->|gate fail + max retries| RB[Rollback]:::fail
  R --> I[Improve]:::ok
  I -->|candidate promoted| C
```

Every task — code fix, research synthesis, blog post, ops runbook — runs through these 6 stages. The recipe (`workflow.yaml`) defines the node graph inside `Execute`. Everything else is framework.

---

## 8 Node-Type Interfaces

Nodes are the roles that agents play inside the `Execute` stage. Each role maps to an interface contract (input shape, output shape, verifier contract) defined in `schemas/workflow.schema.json`.

| Node type | Responsibility | Typical model lane |
|---|---|---|
| `planner` | Decompose task into subtasks; emit objective, assumptions, artifact contract, verifier contract. A plan is not complete until it names how success is checked. | `planner` |
| `researcher` | Gather relevant source material, prior runs, known failure modes. Output is a bounded context pack — not raw dumps. | `researcher`, `glm_lens`, `kimi_lens`, `codex_lens`, `opus_lens`, `minimax_lens` |
| `implementer` | Produce the artifact (patch, doc, config, report). Reads planner output + researcher context. | `implementer` or `worker` |
| `reviewer` | Challenge the artifact against requirements. Write structured feedback. Cannot approve its own work. | `reviewer` |
| `verifier` | Run deterministic probes (typecheck, tests, schema diff, health probe, coverage). Binary pass/fail. | `verifier` |
| `reflector` | Extract textual gradients from this execution trace. Link failures to workflow steps. Suggest improvements. | `reflector` |
| `publisher` | Commit, merge, push, or otherwise finalize the artifact. Runs only after all gates pass. | `publisher` |
| `rollback` | Revert artifact to last known-good state. Preserve worktree for post-mortem inspection. Never destroy. | `rollback` |

---

## 6 Edge-Type Semantics

Edges in `workflow.yaml` carry typed semantics. The executor enforces them.

| Edge type | Meaning | Example |
|---|---|---|
| `depends_on` | Target node cannot start until source completes and its output is available | `implementer depends_on researcher` |
| `supplies_context_to` | Source output is injected into target's context pack, even if not a hard dependency | `planner supplies_context_to verifier` |
| `verifies` | Source verifier node gates target node's continuation | `verifier verifies implementer` |
| `blocks` | Source failure prevents target from running entirely | `scope_gate blocks implementer` |
| `retries` | On failure, target re-runs source with correction context appended | `verifier retries implementer` |
| `escalates_to` | On failure beyond max retries, route to human gate or escalation inbox | `verifier escalates_to human_gate` |

---

## Gate Types

Gates are the checkpoints inside `Verify`. They fire in order; first failure aborts (unless the gate config specifies `continue_on_fail`).

| Gate type | Triggered by | Blocks unless |
|---|---|---|
| `deterministic_verifier` | Script exit code + artifact shape check | Script exits 0 AND artifact matches contract |
| `reviewer_gate` | LLM reviewer JSON output | `approved: true` in structured response |
| `human_gate` | Configured on high-risk rungs (6–7 of autonomy ladder) | Human writes APPROVE to inbox file |
| `budget_gate` | Cumulative cost tracker | `model_costs` total < configured `budget_usd` |
| `scope_gate` | File-path ownership registry | No two nodes claim the same file path |
| `deployment_gate` | Deployment-specific checks (schema diff, rollback proof, health probe) | All deployment probes pass |
| `liveness_gate` | Runtime health and availability probes | Required services/providers respond inside timeout |

Gates are registered at boot via `lib/gate_registry.sh:gate_register`. Custom gates are supported — see [EXTENSION.md](EXTENSION.md).

---

## 8 Memory Namespaces

Memory is scoped. Agents receive only the namespaces relevant to their role, assembled by `lib/context_assembler.sh` to a bounded token budget.

| Namespace | Table(s) | What it holds |
|---|---|---|
| `task_memory` | `task_contexts` | Kickoff, objective, decomposition, artifact contract for this run |
| `workflow_memory` | `workflow_versions`, `workflow_candidates` | Versioned workflow graphs; candidate proposals |
| `agent_performance_memory` | `agent_versions`, `agent_run_stats` | Per-agent model, prompt hash, success rate, cost profile |
| `failure_memory` | `failure_records` | Structured failure events with workflow node + cause |
| `recovery_memory` | `recovery_records` | What worked after which failure — retry strategies |
| `user_preference_memory` | `user_preferences` | Style, verbosity, review depth, risk tolerance |
| `artifact_memory` | `artifact_records` | Produced artifacts with content hash, verifier result, run linkage |
| `benchmark_memory` | `benchmark_tasks`, `benchmark_results` | Known eval tasks + per-version results |

All writes carry provenance: `run_id`, `task_id`, `agent_version_id`, `ts`. A lesson without evidence is a guess.

---

## Task-Class Registry

Task classes are YAML definitions under `${MINI_ORK_HOME}/config/task_classes/`. Each file names a class, its artifact type, required verifiers, and gate policy.

```yaml
# recipes/code-fix/task_class.yaml
name: code_fix
description: "Single-patch code change: bug fix, small feature, refactor of a single function"
artifact_contract_ref: artifact_contract.yaml
default_workflow: workflow.yaml
risk_class: medium

matches:
  keywords: [fix, bug, patch, update, refactor]
  path_globs: ["**/*"]
  regex: []

default_gates:
  - scope_gate
  - budget_gate
  - deployment_gate
```

Validated against `schemas/task_class.schema.json` as the target contract. Built-in classes include `code_fix`, `bdd_first_delivery`, `docs`, `research_synthesis`, `blog_post`, `ui_audit`, `db_migration`, `ops_runbook`, `post_mvp_delivery`, and `recursive_self_improve`.

---

## Workflow Registry

Workflows are versioned DAGs per recipe. Each recipe ships a `workflow.yaml` that declares nodes, edges, and verifier bindings.

```yaml
# recipes/code-fix/workflow.yaml
version: "0.1.0"
task_class: code_fix
description: "Single-patch code fix with typecheck, test, and reviewer gates"

nodes:
  - name: planner
    type: planner
    model_lane: planner
    prompt_ref: prompts/planner.md
    dispatch_mode: serial

  - name: implementer
    type: implementer
    model_lane: worker
    prompt_ref: prompts/implementer.md
    dispatch_mode: serial

  - name: typecheck
    type: verifier
    prompt_ref: null
    verifier_ref: verifiers/typecheck.sh
    dispatch_mode: serial

  - name: reviewer
    type: reviewer
    model_lane: reviewer
    prompt_ref: prompts/reviewer.md
    dispatch_mode: serial

  - name: publisher
    type: publisher
    prompt_ref: null
    dispatch_mode: serial

edges:
  - { from: planner,     to: implementer, edge_type: depends_on }
  - { from: implementer, to: typecheck,   edge_type: verifies }
  - { from: typecheck,   to: reviewer,    edge_type: depends_on }
  - { from: reviewer,    to: publisher,   edge_type: depends_on }
```

The live recipes are the executable contract today. `schemas/workflow.schema.json` is the target validation contract and needs alignment with current fields such as `verifier_ref` and recipe-specific human-decision edges.

---

## State.db Schema Overview

17 migrations. Full DDL in [SCHEMA.md](SCHEMA.md).

| Migration | Tables added | Purpose |
|---|---|---|
| `0001_core.sql` | `runs`, `tasks`, `events`, `model_costs` | Core run lifecycle |
| `0002`-`0008` | sessions, tickets, expected features, research, validation, reflection basins | Early orchestration and validation layers |
| `0009_memory_namespaces.sql` | task/workflow/agent/failure/recovery/user/artifact memory tables | Memory namespaces |
| `0010_benchmarks.sql` | benchmark tables | Benchmark memory namespace |
| `0011_evolution.sql` | gradients, patterns, candidates, evolution runs | Reflection + evolution pipeline |
| `0012_safety.sql` | audit, quarantine, version registry | Governance layer |
| `0013`-`0015` | task runs, execution traces, panel topology telemetry | Run and panel measurement |
| `0016_recursive_orchestration.sql` | spawn lineage and recursive run tracking | Parent/child mini-ork delegation |
| `0017_self_improve_learning.sql` | self-improvement learning records | Recursive self-improvement memory |

---

## Lifecycle Sequence

```mermaid
sequenceDiagram
  autonumber
  participant U as User
  participant CLI as mini-ork
  participant CL as classify
  participant PL as plan
  participant EX as execute (nodes)
  participant VR as verify (gates)
  participant RF as reflect
  participant IM as improve
  participant DB as state.db

  rect rgb(31, 58, 110)
    U->>CLI: mini-ork run code-fix kickoff.md
    CLI->>CL: classify kickoff
    CL->>DB: INSERT task (class, risk, contracts)
  end

  rect rgb(31, 58, 110)
    CL->>PL: plan task
    PL->>DB: INSERT task_context (objective, decomp, artifact_contract)
  end

  rect rgb(31, 77, 31)
    loop for each workflow node (DAG order)
      PL->>EX: dispatch node
      EX->>DB: INSERT trace event (node_id, prompt_hash, cost)
      EX->>DB: write artifact_record
    end
  end

  rect rgb(31, 77, 31)
    EX->>VR: fire gates in sequence
    VR->>DB: INSERT gate_result
  end

  alt all gates pass
    rect rgb(31, 77, 31)
      VR->>EX: publisher node runs
      EX-->>U: artifact committed
    end
  else gate fail, retries remain
    rect rgb(90, 66, 22)
      VR->>EX: retries edge — re-run implementer with correction context
    end
  else gate fail, max retries
    rect rgb(90, 26, 26)
      VR->>EX: escalates_to — rollback node OR human_gate
      EX->>DB: UPDATE task SET status=escalated
      EX-->>U: write INBOX/<task-id>.md
    end
  end

  rect rgb(31, 77, 31)
    EX->>RF: reflect (background)
    RF->>DB: INSERT textual_gradients, pattern_records
  end

  rect rgb(31, 58, 110)
    RF->>IM: improve
    IM->>DB: INSERT workflow_candidates
    IM->>IM: benchmark_suite runs candidates
    IM->>DB: promotion_gate decision → version_registry
    IM->>DB: INSERT audit_log (promote/quarantine)
  end
```

---

## Failure Recovery

**Retry via `retries` edge** (automatic, within a run):
- Triggered when a verifier gate fires `FAIL` and `iter < max_iters`.
- `lib/context_assembler.sh` packs correction context (verifier output + reviewer feedback) into the implementer's next prompt.
- `lib/healer.sh` re-invokes the implementer node with the augmented context.

**Rollback via `rollback` node** (on max-retry exhaustion):
- `lib/branch-quarantine.sh` preserves the worktree — never destroys it.
- Full iter trace + gradient log readable in `state.db`.
- Human-readable escalation written to `${MINI_ORK_INBOX}/<task-id>-<ts>.md`.

**Escalation to `human_gate`** (on deployment-gate or high-rung autonomy triggers):
- Human writes `APPROVE` or `REJECT` to the inbox file.
- Loop polls for the signal; timeout config in `config/safety.yaml`.

---

## Self-Evolution Flow

```mermaid
flowchart TD
  classDef mem   fill:#1f3a6e,stroke:#7aa6ff,stroke-width:2px,color:#fff
  classDef proc  fill:#1f4d4d,stroke:#7adada,stroke-width:2px,color:#fff
  classDef gate  fill:#5a4416,stroke:#ffb84d,stroke-width:2px,color:#fff
  classDef ok    fill:#1f4d1f,stroke:#6bd66b,stroke-width:2px,color:#fff
  classDef fail  fill:#5a1a1a,stroke:#ff6b6b,stroke-width:2px,color:#fff

  T[ExecutionTrace]:::mem --> G[GradientExtractor]:::proc
  G --> TG[TextualGradient]:::mem
  TG --> PS[PatternStore]:::proc
  PS --> PR[PatternRecord]:::mem
  PR --> GE[GroupEvolver]:::proc
  GE --> WC[WorkflowCandidate]:::mem
  WC --> BS[BenchmarkSuite]:::proc
  BS --> PG{PromotionGate}:::gate
  PG -->|utility_delta > 0 AND benchmarks pass| VR[VersionRegistry promoted]:::ok
  PG -->|fail| QR[VersionRegistry quarantined]:::fail
  VR -->|next run| T
```

Self-evolution is evidence-gated: a candidate must beat the current version on the benchmark suite before promotion. Quarantined versions cannot be re-promoted without `version_clear_quarantine`. Every promote/quarantine/rollback writes to `audit_log` (append-only, enforced by sqlite trigger). See [SAFETY.md](SAFETY.md).
