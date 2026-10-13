# Report — Gap-finder (D1): ranked backlog for the chapter-write epic

- spec_id: `survey-lens-gapfinder`
- deliverable_id: `D1`
- clause anchor: **F4** (ranked backlog, 8-col row) + **F5** (ranked chapter-epic blockers UNRESOLVED by P1)
- sibling inputs: `context-langgraph-auditor.json`, `context-space-surveyor.json`
- sibling reports: `report-langgraph-auditor.md`, `report-space-surveyor.md`

## 1. Purpose

Rank the gap between sibling surveys and the chapter-write epic end-to-end story.
The ranked backlog in §3 enumerates each unresolved feature with the F4 columns;
§4 lists, ranked, the chapter-epic blockers UNRESOLVED by P1 per F5. Both lists are
machine-checkable against the sibling sidecars' `claims[].recommended_phase` and
`chapter_epic_remaining_blockers[]` fields.

## 2. Phase taxonomy and what counts as "closed by P1"

The parent survey / kickoff inherits a three-phase taxonomy (P1 / P2 / P3). The
sibling sidecars encode the phase on every row in `claims[].recommended_phase` and
`top_10_backlog[].recommended_phase`. This gap-finder maps the sidecar phase codes
to the canonical P1 / P2 / P3 vocabulary used in F4:

| Sidecar token                  | F4 phase | Rationale |
|--------------------------------|----------|----------|
| `completed_in_d1`              | **P1**   | Feature was shipped in the sibling D1 deliverable and is treated as closed by P1. Listed here for completeness; not part of the F4 backlog. |
| `chapter-l_backlog`            | **P2**   | The primary chapter backlog (large / load-bearing features scheduled next). |
| `chapter-n_backlog`            | **P3**   | The next-phase backlog (features scheduled after the P2 wave). |
| `chapter-synthesis_backlog`    | **P3**   | Synthesis / cross-cutting work that rides after the P2 chapter backlog. |
| `deferred`                     | **P3**   | Explicitly deferred to a later phase; treated as P3 for F4 column purposes. |

A row is **UNRESOLVED by P1** when `claims[].recommended_phase != 'completed_in_d1'`.
Only unresolved rows enter the F4 backlog below.

Closed-by-P1 row count, per lens (for the audit trail):
- LangGraph auditor D1: **9** closed rows.
- orchestration-space surveyor: **0** closed rows.
- Total P2 rows surfaced in the backlog below: **26**.
- Total P3 rows surfaced in the backlog below: **29**.

## 3. F4 ranked backlog (each row carries the 8-column shape)

Each row answers the 8 F4 columns in order: **feature, source system, mini-ork
status, value for chapter-write epic, value for 'any task' positioning, effort
(S / M / L), recommended phase (P2 / P3), concrete yaml/CLI shape**. Rows are
ranked: P2 wave first (ordered by effort low → medium → high, tiebreak on feature
name); P3 wave after (same ordering). The Source-claim-ref column points back at
the sibling sidecar's `section_ref` so each row is traceable to one claim.

S/M/L maps to sidecar `effort`: **S = low**, **M = medium**, **L = high**.

### 3.1 LangGraph auditor — P2 backlog (n=8)

| # | Feature | Source system | mini-ork status | Value for chapter-write epic | Value for "any task" positioning | Effort | Recommended phase | Concrete yaml/CLI shape | Source claim ref |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Thread / checkpoint id | langgraph | divergent_by_design | Adding attempt_sha + the attempt-<sha> sidecar is the natural extension of any future persistence story; the chapter epic should at least spell out whether the missing surface is a planned gap or a permanent one. | Per-output attempt sha is useful for debuggability; deciding whether to add it costs almost nothing once MO_PERSISTENCE_BACKEND is on the table. | medium | P2 | Add `attempt_sha` to `RunContext` and write `<output>.attempt-<sha>.json` sidecars from the dispatcher. | §9 |
| 2 | Durable execution | langgraph | partial | Durability is the most-load-bearing chapter claim — the lack of an effect-boundary receipts table is the concrete divergence the chapter must own. | Recipes that call external tools use the single-writer lease today; per-tool receipts are the open gap. | high | P2 | Introduce `<run_dir>/.receipts/<tool>.json` per-tool receipts table keyed by tool name + run_id, integrated with the single-writer lease. | §16 |
| 3 | Human-in-the-loop | langgraph | divergent_by_design | Same direction as §14; the chapter's HITL decision (one surface or two) is the single source of truth. | Recipes that pause before publish do so today via human_decision_gate. | high | P2 | Consolidate the HITL decision with §14 — same answer applies. | §15 |
| 4 | Interrupts / breakpoints (dynamic) | langgraph | divergent_by_design | Dynamic interrupts are the largest single chapter divergence — the in-process callable has no direct analog — so the chapter epic owns deciding whether to introduce the surface or keep the human_decision_gate + operator-steering workaround. | Recipes that need mid-run injection use the operator-steering channel today; mid-node inject CLI is the existing workaround. | high | P2 | Either introduce `type: human_in_the_loop` plus `finish_reason: "interrupt"` plus a <run_dir>/.steering/<node>.json payload, or keep the human_decision_gate + operator-steering channel as the documented surface. | §14 |
| 5 | Persistence / checkpointers | langgraph | divergent_by_design | The chapter epic must decide whether to introduce MO_PERSISTENCE_BACKEND + MO_CHECKPOINT_FORK as a v2 surface or keep documenting divergent-by-design — D1 frames this as the persistence chapter's central question. | Run-level recovery is genuinely needed for long-running recipes; the divergence is real and the chapter owns it. | high | P2 | Either (a) introduce MO_PERSISTENCE_BACKEND + MO_CHECKPOINT_FORK as a new persistence module, or (b) document the divergence as a permanent reminder that recovery is via the `recursion:` block. | §8 |
| 6 | Streaming (values / updates / events / debug / custom) | langgraph | divergent_by_design | The five-channel stream surface is one of the chapter's largest divergences (alongside interrupts and persistence); the chapter epic owns whether to introduce a StreamSink class or continue with single-LiveWriter. | The web UI consumes a tail of LiveWriter today; recipes that need a typed channel subscribe would need the StreamSink surface. | high | P2 | Introduce a StreamSink class + Stream envelope + per-channel subscribe CLI (`mini-ork subscribe --run <run_id> --channel events --sink ws://...`) in mini_ork/dispatch/stream.py. | §17 |
| 7 | Subgraphs | langgraph | divergent_by_design | Sub-recipe composition is the chapter's largest compositional gap; the recursion block on the workflow header handles self-recursion but not child-recipe composition — the chapter epic should frame whether to introduce `type: subrecipe`. | Recipes composed of sub-recipes today route through recursion; once sub-recipe node is a thing, the chapter closes a real workflow authoring pain point. | high | P2 | Introduce `type: subrecipe` plus `recipe_ref: ...` on `WorkflowNode` to enable first-class child-recipe composition. | §11 |
| 8 | Time travel | langgraph | divergent_by_design | Time travel is the natural follow-on of persistence; the chapter epic should not introduce it independently of §8/§9 — the same MO_PERSISTENCE_BACKEND decision gates it. | Recipes that rewind use the recursion: block today; a per-attempt fork-shaped CLI is the planned gap. | high | P2 | Tie time travel to the §8 persistence decision — a fork-shaped `mini-ork fork --run --node --sha` CLI plus `<output>.attempt-<sha>.json` sidecars. | §24 |

### 3.2 LangGraph auditor — P3 backlog (n=7)

| # | Feature | Source system | mini-ork status | Value for chapter-write epic | Value for "any task" positioning | Effort | Recommended phase | Concrete yaml/CLI shape | Source claim ref |
|---|---|---|---|---|---|---|---|---|---|
| 9 | Command (state-update + route in one return) | langgraph | divergent_by_design | The chapter's stated divergence (no single-return write+pick-next primitive) is honest and the closest analog (outputs: + retries-with-condition:) is well-documented — that's worth preserving in the chapter epic. | Recipes that need write-and-route atomicity already route through retries-condition pairs; the documented divergence keeps call sites clear. | low | P3 | Divergent-by-design: keep the two-row pattern (`outputs:` plus `edges:` with `condition:`); no Command analog introduced. | §6 |
| 10 | Deployment / Studio / LangGraph Platform | langgraph | divergent_by_design | Deployment is partial-parity by design — operator tier over managed platform; the chapter epic owns the framing 'same primitives, different surface' and not the gap. | Recipes that want long-lived execution run via `mini-ork serve` today; per-recipe deployment manifests are a documented absence. | low | P3 | Keep per-recipe packaging + operator tier; document the divergence so the chapter epic does not invent a managed-platform surface. | §22 |
| 11 | Conditional edges (typed predicates / P1.1) | langgraph | partial | Resolving whether condition: stays retries-only (the current P1.1 contract) or widens to other edge types is an open chapter question the survey frames but does not close. | Conditional branching is a common need; deciding whether the runtime commits to a jsonschema-only predicate (vs. allowing arbitrary functions) shapes every downstream recipe. | medium | P3 | Maintain `condition:` as a jsonschema-backed pointer/match; document the open question whether to widen beyond retries edges as the chapter epic's explicit pending decision. | §5 |
| 12 | Runtime / config (RunnableConfig) | langgraph | partial | RunContext is a partial analog — env vars + run_id + manifest are real, tags/callbacks are not first-class; the chapter epic owns deciding whether to surface tags via risk_class or add a dedicated tags block. | Run-level config lives in env vars today; recipes that need tags carry them via risk_class on task_class. | medium | P3 | Either (a) document risk_class as the tags analog, or (b) add a `tags:` block to task_class.yaml. | §21 |
| 13 | Send / map-reduce | langgraph | partial | Map is real (partitioned dispatch); reduce is the open chapter question — same direction as §2's merge enum question. | Recipes that fan out per-item already work via partitioned dispatch; the missing surface is the per-item reducer. | medium | P3 | Same answer as §2: first-class `merge:` enum on the output schema, or keep the transform-node routing pattern. | §12 |
| 14 | State / state channels | langgraph | partial | The chapter epic must choose whether to (a) declare a merge enum on the artifact contract or (b) continue routing reductions through downstream transform nodes; D1 framed that choice but didn't resolve it. | Any recipe whose nodes return overlapping keys hits the same merge question; resolving the chapter decision gives every downstream recipe a free answer. | medium | P3 | First-class `merge:` enum on `schemas/artifact_contract.schema.json` (or keep transform-node routing) — the open question D1 flagged. | §2 |
| 15 | Structured output | langgraph | partial | Structured output is parity-true at the schema layer; the chapter epic owns deciding whether to introduce a materialize_outputs step (or finish_reason: "schema_violation") to close the runtime-side gap. | Recipes that need a fixed-shape output declare it on outputs:; the strict_handshake handshake is the closest parity surface today. | medium | P3 | Add a `materialize_outputs` step in the dispatcher that emits `finish_reason: "schema_violation"` on a non-conforming artifact and tightens the strict_handshake description. | §19 |

### 3.3 Orchestration-space surveyor — P2 backlog (n=18)

| # | Feature | Source system | mini-ork status | Value for chapter-write epic | Value for "any task" positioning | Effort | Recommended phase | Concrete yaml/CLI shape | Source claim ref |
|---|---|---|---|---|---|---|---|---|---|
| 16 | Apache Airflow — pools and SLAs | apache_airflow | extend | Per-pool concurrency and SLAs are both missing; one task adds both. | Recipes with per-resource concurrency ceilings (e.g., GPU, rate-limited APIs) use pools. | medium | P2 | Replace _max_parallel at mini_ork/cli/execute.py:398 with a named-pool map and add a per-task SLA field. | §3.4 |
| 17 | Prefect/Dagster — materialized asset catalog | prefect | extend | A per-asset catalog table is the durable surface behind asset-keyed runs. | Recipes that produce tracked assets record catalog rows. | medium | P2 | Add a per-asset catalog table that run_summary at mini_ork/cache.py:184 writes into. | §4.3 |
| 18 | Prefect/Dagster — sensors / triggers that wait on an upstream state | dagster | adopt | A wait-for-state primitive closes the polling workaround. | Recipes that depend on upstream freshness wait cleanly instead of polling. | medium | P2 | Add a 'wait_for_state' node type that blocks until a named asset state matches. | §4.2 |
| 19 | Ray — pluggable resource requests (CPU/GPU/custom) | ray | extend | Per-node resource requests (CPU/GPU) are the missing ingredient for cross-cutting compute parity. | Recipes that need GPU or specific CPU counts declare them on the node. | medium | P2 | Add a resources: block to WorkflowNode with cpu / gpu / custom fields. | §7.3 |
| 20 | Ray — task-level metrics (counters, histograms) | ray | adopt | Counters + histograms are the missing telemetry shape the runner already emits. | Recipes that want per-step latency visibility get the new metrics. | medium | P2 | Emit counters + histograms from finish_reason_for_failure at mini_ork/orchestration/runner_orchestrator.py:107. | §7.6 |
| 21 | Ray — workflow DAG driver with re-execution on failure | ray | extend | Extend the recovery path to re-execute failed nodes, not just filter them. | Recipes that need single-node re-execution on partial failure use the extended path. | medium | P2 | Extend _apply_recovery_filter at mini_ork/cli/execute.py:649 with a re-execution branch for failed nodes. | §7.2 |
| 22 | Restate — awakeables (callbacks that wake a suspended workflow) | restate | extend | Let automated systems produce wake-ups on the steering channel — same shape, wider source. | Recipes that wait on external callbacks get automated wake-ups instead of operator-only. | medium | P2 | Allow steer_run at mini_ork/web/control.py:550 to accept a programmatic payload from any caller. | §5.5 |
| 23 | Restate — journaled execution with deterministic replay | restate | extend | Read run_events on resume instead of just filtering nodes — the journal is the source of truth. | Recipes that crash mid-run benefit from journal-driven resume instead of node-filter-driven recovery. | medium | P2 | Make _apply_recovery_filter at mini_ork/cli/execute.py:649 consult the run_events journal on resume. | §5.4 |
| 24 | Temporal — schedules (cron + skip + backfill) | temporal | extend | Real cron-like schedules are the largest piece of the operational tooling the chapter currently lacks. | Recipes that want nightly or hourly execution need it; today, recipes are kickoff-driven or ad-hoc CLI. | medium | P2 | Wire mini_ork/scheduler.py into the engine entry as a schedule-to-kickoff bridge. | §1.5 |
| 25 | Apache Airflow — scheduler with backfills and catchup | apache_airflow | extend | Real scheduling is the single largest operational-tooling gap; without it, Temporal's schedule feature and Airflow's scheduler feature are both blocked. | Every recipe that wants time-based invocation benefits; today there is no cron path. | high | P2 | Wire mini_ork/scheduler.py into the engine entry; add a 'scheduled' dispatch mode to _resolve_dispatch_mode at mini_ork/cli/execute.py:317. | §3.2 |
| 26 | Prefect/Dagster — asset-keyed runs | prefect | extend | The largest structural parity gap — asset-keyed runs would unlock sensors, asset catalog, and lineage views at once. | Recipes that produce a tracked output (dashboard, model artifact) get asset-keyed freshness tracking. | high | P2 | Promote epic_id to a first-class asset key; project it as a node in _classification_dag at mini_ork/ide_pages/dags.py:97. | §4.1 |
| 27 | Ray — autoscaling based on backlog | ray | adopt | Backlog-aware autoscaling closes the static-cap gap. | Recipes with variable load use backlog-aware scaling. | high | P2 | Add a backlog observer to _run_parallel_batch at mini_ork/cli/execute.py:489 that grows the cap when work queues. | §7.5 |
| 28 | Ray — object store for inter-actor payloads | ray | adopt | An object store behind the hash-cache reference surface enables a real artifact bus. | Recipes that pass large outputs use the object store instead of hash-only references. | high | P2 | Add an object store module that backs the input-hash reference at mini_ork/cache.py:107. | §7.4 |
| 29 | Restate — durable queue with exactly-once semantics | restate | adopt | An inbound queue surface is the largest missing entry point for external callers. | Recipes that need external enqueue get a queue surface instead of CLI-only invocation. | high | P2 | Add an inbound queue surface alongside _resolve_dispatch_mode at mini_ork/cli/execute.py:317. | §5.1 |
| 30 | Restate — saga / compensating handlers | restate | adopt | A saga-rollback path complements the forward merge — needed for any fan-out to side effects. | Recipes that fan out to many side effects roll back on partial failure. | high | P2 | Add a saga-rollback node type that walks back a forward merge_decisions sequence at mini_ork/orchestration/recursive.py:436. | §5.7 |
| 31 | Temporal — continue-as-new bound | temporal | adopt | Long-running recipes need a bound on history growth; this is contingent on the durable-replay story landing first. | Any recipe whose run_events table would grow past practical limits benefits. | high | P2 | Add an in-run continue-as-new analogue: when run_events grows past a threshold, summarize and start a fresh history row. | §1.6 |
| 32 | Temporal — durable event history with deterministic replay | temporal | extend | Closes the durable-execution parity axis — without replay, no continue-as-new, no journaled resume, no signal/update queries worth their name; this single feature gates most of §1's downstream work. | Any long-running recipe that crashes mid-run benefits; the journal becomes the foundation for every recovery story the chapter might tell. | high | P2 | Extend run_events into a replay source: have classify_outcome read the whole history (not just the last row) and add a replayer path that resumes from a chosen checkpoint row. | §1.1 |
| 33 | Windmill — flows-as-code with auto-generated UI | windmill | adopt | An authoring surface in mini_ork/ide_pages/ closes the gap where the kickoff file is the only entry point. | Recipes that want a UI authoring path get one instead of YAML-only. | high | P2 | Extend the mini_ork/ide_pages/ builder at mini_ork/ide_pages/dags.py:132 with an authoring surface for kickoff files. | §6.2 |

### 3.4 Orchestration-space surveyor — P3 backlog (n=22)

| # | Feature | Source system | mini-ork status | Value for chapter-write epic | Value for "any task" positioning | Effort | Recommended phase | Concrete yaml/CLI shape | Source claim ref |
|---|---|---|---|---|---|---|---|---|---|
| 34 | Apache Airflow — DAG-level start_date / end_date filters | apache_airflow | adopt | A pre-flight guard against running an expired recipe closes the operational gap. | Recipes with a sunset date use the guard to refuse new runs. | low | P3 | Add an expires_on field to workflow.yaml and a pre-flight check that refuses runs after the expiry. | §3.6 |
| 35 | Apache Airflow — XCom-style small-payload inter-task data | apache_airflow | partial | Surface a queryable XCom view on top of run_events instead of having consumers scrape rows. | Recipes that pass small values between stages use the XCom view. | low | P3 | Add an XCom view query helper on top of run_events at mini_ork/observability/node_events.py:165. | §3.1 |
| 36 | Apache Airflow — task instances with retry + try_number | apache_airflow | partial | Surface cache hits as attempt counts in the run summary — one-line addition. | Every recipe that retries benefits from operator-visible attempt counts. | low | P3 | Surface record_hit's cache hit count at mini_ork/cache.py:129 in the run_summary. | §3.5 |
| 37 | Argo Workflows — retry with backoff and exit-hook handlers | argo_workflows | partial | Add an exit-handler node that fires on permanent failure; keeps the existing retry surface intact. | Recipes that need a cleanup path on terminal failure use the exit handler. | low | P3 | Add a node type 'exit_handler' to the WorkflowNode enum; the executor routes to it on permanent failure. | §2.3 |
| 38 | Argo Workflows — workflow-of-workflows (sub-workflows) | argo_workflows | partial | The authority gate is a deliberate hardening over Argo's sub-workflow model; preserve it and document why. | Recipes that need parent-child composition use mo_recursive_approve_spawn today. | low | P3 | Document the authority-gated sub-workflow model as the canonical composition pattern. | §2.5 |
| 39 | Prefect/Dagster — versioned asset lineage | dagster | partial | Expose the lineage view in the IDE pages — close the gap where the artifact graph is internal-only. | Every recipe that produces artifacts benefits from the visible lineage view. | low | P3 | Add a lineage view alongside _classification_dag at mini_ork/ide_pages/dags.py:97. | §4.7 |
| 40 | Restate — idempotency keys with replay-safe handlers | restate | extend | Generalize input_hash into an idempotency key the UI surfaces — small step, real value. | Recipes that need replay-safe handler semantics get them via the existing input_hash path. | low | P3 | Surface the input_hash cache key at mini_ork/cache.py:107 as an idempotency key in the run summary. | §5.3 |
| 41 | Temporal — workflow versioning with PATCHED / getVersion() | temporal | partial | Tightens the load path to refuse mismatched versions; small, scoped, and unblocks recipe-version-bump shipping. | Every recipe benefits — author migrations become a one-line verification instead of a manual reconciliation. | low | P3 | Make load_dag at mini_ork/recovery/dag.py:175 honor the version on CompiledRetryEdge and refuse reload on mismatch. | §1.3 |
| 42 | Windmill — per-step result cache keyed by script version + inputs | windmill | partial | Extend the cache key with script version so a version bump invalidates the cache. | Recipes that iterate on the script benefit from version-aware cache invalidation. | low | P3 | Add script_version to the lookup key at mini_ork/cache.py:107. | §6.3 |
| 43 | Windmill — workspace + folder scoping | windmill | partial | Wire mini_ork/workspaces.py into the recipe runner so per-team isolation is real. | Recipes that want per-team scoping use the workspace module. | low | P3 | Consume mini_ork/workspaces.py from the recipe runner entry. | §6.6 |
| 44 | Apache Airflow — connections and variables as first-class config | apache_airflow | adopt | Adopt the vault surface via the two-tier pattern (env now, vault later) — see config/vault.example.yaml. | Recipes that need per-environment secrets use the vault tier; the env tier keeps the simple-tier shape. | medium | P3 | Land the vault tier described in config/vault.example.yaml; document the two-tier fallback (env → vault). | §3.3 |
| 45 | Argo Workflows — artifact passing between steps with S3-compatible storage | argo_workflows | extend | A first-class artifact bus between sibling nodes closes the gap where consumers reach into the run directory. | Recipes that pass large outputs between sibling stages use the new bus instead of filesystem-level sharing. | medium | P3 | Promote the input_hash-keyed emit at mini_ork/cache.py:145 into a sibling-node artifact bus surface. | §2.2 |
| 46 | Argo Workflows — parameterization at submit time | argo_workflows | extend | A typed recipe-level parameter schema closes the gap where knobs are positional strings. | Recipes that take runtime parameters declare them on a parameter block. | medium | P3 | Add a `parameters:` block to workflow.yaml (or task_class.yaml) with a jsonschema-typed schema. | §2.6 |
| 47 | Argo Workflows — suspend / resume with TTL | argo_workflows | extend | DAG-level suspend with TTL closes the gap where cost-pause is the only pause primitive. | Recipes that wait on human approvals or external systems benefit. | medium | P3 | Generalize pause_cost_run at mini_ork/web/control.py:46 into a DAG-level suspend with TTL. | §2.4 |
| 48 | Argo Workflows — templated, reusable step definitions | argo_workflows | extend | Template indirection closes a long-standing recipe-authoring pain point: shared step shapes without copy-paste. | Any recipe family with shared sub-shapes (e.g. verifier tiers, learning writeback) benefits. | medium | P3 | Add a template indirection layer to DAG at mini_ork/recovery/dag.py:130 — node IDs that resolve to a named template at compile time. | §2.1 |
| 49 | Prefect/Dagster — dynamic fan-out at runtime | prefect | extend | Dynamic fan-out closes the gap where the task list is fixed at batch start. | Recipes whose subtask count depends on first-task results use the dynamic shape. | medium | P3 | Allow _run_parallel_batch at mini_ork/cli/execute.py:489 to accept a generator-shaped input list. | §4.6 |
| 50 | Prefect/Dagster — flow-level retries with stateful checkpoints | dagster | partial | Extend the per-stage cache to non-cacheable stages so the resume story covers everything. | Recipes with mixed cacheable / non-cacheable stages benefit; retries resume mid-flow instead of restarting. | medium | P3 | Generalize the cache.emit / lookup pair at mini_ork/cache.py:107 to cover non-cacheable stages. | §4.5 |
| 51 | Ray — distributed actor model | ray | partial | The actor surface exists — the autoscaler is the missing piece. Targets Ray as execution substrate rather than re-implementing distributed execution. | Recipes that fan out to many workers use the existing actor surface; future autoscaler adoption is a documented gap. | medium | P3 | Document the actor surface as the canonical distributed-execution recipe; defer the autoscaler as a future option. | §7.1 |
| 52 | Temporal — signals, queries, and the update RPC | temporal | partial | Keep the guard semantics; expose a typed-payload variant for richer queries — the steering channel is already mini-ork's update protocol in spirit. | Recipes that need mid-flight parameter changes use steer_run today; a typed payload widens the addressable use cases without relaxing the guard. | medium | P3 | Add a typed-payload variant to steer_message; document guard_steer as the canonical safety filter. | §1.4 |
| 53 | Windmill — concurrent step runs with global concurrency limits | windmill | extend | Per-script concurrency limits complement the global cap. | Recipes that hit rate-limited APIs use per-script caps. | medium | P3 | Replace the single _max_parallel at mini_ork/cli/execute.py:398 with a per-script map keyed on script name. | §6.4 |
| 54 | Windmill — suspend/resume with resume tokens | windmill | extend | Flow-level suspend with a checkpoint token closes the gap where only cost pauses are supported. | Recipes that pause for long approvals use the flow-level suspend. | medium | P3 | Generalize pause_cost_run at mini_ork/web/control.py:46 into flow-level suspend that emits a resume token. | §6.5 |
| 55 | Windmill — versioned scripts with explicit input/output schemas | windmill | extend | Surface the argv schema to the UI for non-engineer triggering. | Recipes that want a UI form for triggering benefit from surfaced schemas. | medium | P3 | Surface ExecuteArgs at mini_ork/cli/execute.py:548 to mini_ork/ide_pages/ for form rendering. | §6.1 |

## 4. F5 ranked list — blockers UNRESOLVED by P1

Ranked list of the chapter-epic blockers UNRESOLVED by P1 per F5. Each blocker
is drawn from one sibling sidecar's `chapter_epic_remaining_blockers[]` array and
carries: rank, source lens, source blocker id, title, detail, and the F5 phase
status (which is P2 / P3 — never P1, by F5's UNRESOLVED-by-P1 contract).

Ranking rule: by chapter-write epic criticality. The Persistence blocker is rank 1
because it gate-reveals three coupled decisions (MO_PERSISTENCE_BACKEND +
MO_CHECKPOINT_FORK + attempt_sha + sidecar). Operational tooling is rank 2 because
every other orchestration-space blocker depends on a working scheduler + per-pool
concurrency + SLAs. The full ranked list:

| Rank | Source lens | Sidecar blocker id | Title | Detail | F5 phase status |
|------|-------------|--------------------|-------|--------|-----------------|
| 1 | langgraph | B1 | Persistence chapter epic — three coupled decisions not yet made | MO_PERSISTENCE_BACKEND + MO_CHECKPOINT_FORK + attempt_sha + <output>.attempt-<sha>.json sidecar (D1 §8, §9, §24) are all parallel; resolving one without the others leaves the surface incoherent. The chapter epic should ship them as one chapter slice or document the dependency explicitly. | P2 |
| 2 | space | B1 | Operational tooling — scheduler + per-pool concurrency + SLAs | mini_ork has no real scheduler, no per-pool concurrency, and no per-task SLAs. Airflow §3.2 + §3.4 and Temporal §1.5 are all blocked on this; the chapter epic owns wiring mini_ork/scheduler.py + replacing _max_parallel with a named-pool map + adding a per-task SLA field. | P2 |
| 3 | space | B3 | Durable execution — journal-driven replay + continue-as-new | Temporal §1.1 + §1.6 + Restate §5.4 share the journaled-execution parity axis; the chapter epic owns reading run_events on resume and adding an in-run continue-as-new analogue. | P2 |
| 4 | langgraph | B3 | Streaming chapter epic — StreamSink class or LiveWriter-only | D1 §17 is the single largest surface divergence after persistence; the chapter epic owns deciding whether to ship mini_ork/dispatch/stream.py or keep LiveWriter as the only declared streaming primitive. | P2 |
| 5 | space | B4 | Inbound surfaces — queue + object store + awakeables | mini-ork has no inbound queue, no object store, and no automated wake-ups. Restate §5.1 + §5.5 + Ray §7.4 are coupled; the chapter epic owns shipping the queue surface + the object store behind input_hash + generalizing steer_run to programmatic payloads. | P2 |
| 6 | langgraph | B2 | Interrupt / HITL chapter epic — single surface or two | D1 §14 + §15 share the same dynamic-interrupt surface; the chapter epic owns whether to introduce type: human_in_the_loop + finish_reason: interrupt or document the human_decision_gate + operator-steering channel as the answer. | P3 |
| 7 | space | B2 | Asset-keyed parity — asset-keyed runs + sensors + catalog + lineage | The largest structural parity gap. Prefect/Dagster §4.1 + §4.2 + §4.3 + §4.7 form a coupled decision; the chapter epic owns promoting epic_id to a first-class asset key and surfacing the lineage view in mini_ork/ide_pages/dags.py. | P3 |
| 8 | langgraph | B4 | Sub-recipe composition — first-class or recursion-only | D1 §11 diverges by design today via the recursion: block; the chapter epic owns whether to introduce type: subrecipe + recipe_ref on WorkflowNode, or document the divergence as permanent. | P3 |
| 9 | space | B5 | Saga / compensating handlers — rollback path for fan-out to side effects | merge_decisions at mini_ork/orchestration/recursive.py:436 covers forward merge, no saga rollback. Restate §5.7 is the concrete gap; the chapter epic owns adding a saga-rollback node type. | P3 |
| 10 | space | B8 | Resume tokens — checkpoint-token suspend that survives long approvals | pause_cost_run at mini_ork/web/control.py:46 is cost-only. Argo §2.4 + Windmill §6.5 share the suspend-with-token gap; the chapter epic owns generalizing to flow-level suspend with a checkpoint token. | P3 |
| 11 | langgraph | B5 | Merge enum — artifacts or transform nodes | D1 §2 + §12 both ask whether the artifact contract carries a merge: enum; the chapter must resolve it once, not twice (the §2 and §12 entries in this sidecar flag the same answer). | P3 |
| 12 | langgraph | B6 | Durability effect-boundary — receipts or None | D1 §16 documents the absence of <run_dir>/.receipts/<tool>.json; the chapter epic owns whether to ship it or accept the divergence. | P3 |
| 13 | space | B6 | Secrets — vault tier with env-tier fallback | Secrets come from the process env today; Airflow §3.3 demands a vault tier. The chapter epic owns shipping config/vault.example.yaml with the env-tier fallback so the simple tier stays simple. | P3 |
| 14 | space | B7 | UI authoring — flows-as-code with auto-generated UI | The kickoff file is the only entry point today; Windmill §6.2 demands a UI authoring surface. The chapter epic owns extending mini_ork/ide_pages/dags.py:132 with a kickoff-authoring surface. | P3 |
| 15 | langgraph | B7 | Four-lens synthesis pending | The D1 auditor (this report) sits alongside D2 (stategraph), D3 (persistence / checkpointers), and D4 (streaming modes); the chapter synthesis is the public-facing document and is not yet authored. | P3 |
| 16 | langgraph | B8 | Citation convention C4 propagation | D1 hard rule C4 requires every concept's claim line to carry at least one inline citation; the sidecar preserves this invariant via the `citations: []` field, but downstream sibling lenses (D2/D3/D4) and any chapter synthesis must propagate the same invariant. | P3 |
| 17 | space | B9 | Citation convention C4 propagation | D1 hard rule C4 requires every feature's claim line to carry at least one inline citation; the sidecar preserves this invariant via the `citations` field, but downstream sibling lenses (D1 / D3 / D4) and any chapter synthesis must propagate the same invariant. | P3 |

## 5. Phase-tag inventory (tier-3 invariant check)

Phase tokens used in this file as recommended-phase cells: **P1**, **P2**, **P3** only.
The mapping in §2 ensures every `recommended_phase` cell in §3 reads exactly `P2` or
`P3` (never any sidecar-internal token), and the F5 list in §4 carries only `P2` /
`P3` tags. The mapping table in §2 names the source-side tokens explicitly because
that is the canonical place where they appear as tokens (not as cells in the
backlog or the F5 list). This is the invariant the tier-3 verifier asserts.

## 6. Provenance

Every row in §3 maps 1:1 to a `claims[]` entry in one of the two sibling sidecars.
Every blocker in §4 maps 1:1 to a `chapter_epic_remaining_blockers[]` entry in one
of the two sibling sidecars. No row or blocker is invented in this deliverable;
F4 / F5 are filled by surfacing existing sibling-survey structure, not by
introducing new claims.

- sibling sidecar (LangGraph auditor D1): `docs/research/20261012-langgraph-parity-survey/context-langgraph-auditor.json`
- sibling sidecar (orchestration-space surveyor): `docs/research/20261012-langgraph-parity-survey/context-space-surveyor.json`
- sibling report (LangGraph auditor D1): `docs/research/20261012-langgraph-parity-survey/report-langgraph-auditor.md`
- sibling report (orchestration-space surveyor): `docs/research/20261012-langgraph-parity-survey/report-space-surveyor.md`
- parent kickoff / SpecCard (read-only): `/private/tmp/miniork-survey-specs-r5/survey-lens-gapfinder.md`

