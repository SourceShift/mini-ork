# Feature inventory (synthesis)

Code-grounded catalog of mini-ork as a cloud agent orchestration platform.
Consolidates 4 lens reports (codex, minimax, kimi, glm) into the 7 pillars
from the kickoff. Every feature carries a `file:line` anchor and a
`shipped`|`specced` marker. `[CONSENSUS: N/4]` = number of lenses that
independently surfaced the feature.

## Summary
- Total unique features: 124 (119 shipped + 5 specced/roadmap)
- Consensus 4/4: 4
- Consensus 3/4: 36
- Consensus 2/4: 59
- Single-lens finds: 20
- Pillars: 7 (all ≥3 features)

## Orchestration core (13 features)
- **`run` lifecycle** — classify → profile → plan → execute → verify → reflect → prune, one CLI dispatch — `mini_ork/cli/main.py:557` (`main`), `mini_ork/cli/main.py:308` (`_run_lifecycle`). [CONSENSUS: 3/4] [shipped]
- **Subcommand registry (OCP)** — third-party subcommands register without editing the dispatcher — `mini_ork/cli/main.py:680` (`register_subcommand`), `mini_ork/cli/main.py:677`. [CONSENSUS: 3/4] [shipped]
- **Keyword task classifier** — regex/alias scorer over `config/task_classes/*.yaml`, zero-LLM, lex-first tiebreak — `mini_ork/cli/classify.py:42` (`_score`), `mini_ork/cli/classify.py:80`. [CONSENSUS: 3/4] [shipped]
- **Planner + repair-on-bad-JSON loop** — dispatches planner lane, retries recoverable verdicts up to `MO_PLAN_MAX_REPAIRS` (default 2), preserves forensic raw — `mini_ork/cli/plan.py:669`, `mini_ork/cli/plan.py:53` (`_RECOVERABLE_VERDICTS`). [CONSENSUS: 3/4] [shipped]
- **Profile Q&A / needs_answers gate** — auto-answers via LLM or reads `/dev/tty`; blocks planning when confidence < floor — `mini_ork/cli/plan.py:268` (`_auto_answer_profile`), `mini_ork/web/routes/control.py:54` (`get_profile`/`save_answers`). [CONSENSUS: 3/4] [shipped]
- **Recovery DAG (adjacency + Kahn topo)** — parses `workflow.yaml` nodes/edges; excludes `escalates_to` operator edges from data flow — `mini_ork/recovery/dag.py:81` (`load_dag`), `mini_ork/recovery/dag.py:39`. [CONSENSUS: 3/4] [shipped]
- **Multi-epic scheduler (bounded pool)** — `MO_SCHED_MAX_PARALLEL` (default 3); ready-set dispatch + dep cascade per verdict; `--once` mode — `mini_ork/scheduler.py:1`. [CONSENSUS: 3/4] [shipped]
- **Recipe resolution from task_class** — matches `recipes/<name>/task_class.yaml::name`, `_`↔`-` fallback — `mini_ork/cli/main.py:113` (`resolve_recipe`). [CONSENSUS: 2/4] [shipped]
- **Run-profile generator** — extracts scope/commands/lanes from kickoff → `run_profile.json` with confidence + `ready|blocked_profile|needs_answers` — `mini_ork/cli/main.py:135` (`gen_profile`). [CONSENSUS: 2/4] [shipped]
- **Conductor (meta-policy picker)** — picks topology + lane hints + prompt gates from learning tables; EMA-gained on `realized_score` — `mini_ork/orchestration/conductor.py:1` (`decide_for_epic`). [CONSENSUS: 2/4] [shipped]
- **Idea-tree (epic breakdown persistence)** — recursive tree CRUD for the Conductor's breakdown — `mini_ork/web/routes/idea_tree.py:29`; table `db/migrations/0020_idea_tree.sql`. [CONSENSUS: 2/4] [shipped]
- **Recipe-set (33 recipes)** — each carries `task_class.yaml`+`workflow.yaml`+`artifact_contract.yaml` — `recipes/code-fix/workflow.yaml:33`. [CONSENSUS: 2/4] [shipped]
- **Workflow compiler (artifact-graph)** — compiles nodes+edges+bindings to `CompiledWorkflow`; `consumer`/`system_only` output visibility — `mini_ork/workflow/compiler.py:200` (`compile_workflow`). [CONSENSUS: 1/4] [shipped]

## Heterogeneous model dispatch (18 features)
- **Provider registry (BYO, 5 kinds)** — `anthropic-native|anthropic-compat|openai-compat|codex-native|executable`; 6+ built-in lanes — `mini_ork/dispatch/providers.py:494` (`PROVIDER_KIND_BUILDERS`); `config/providers.yaml:42`. [CONSENSUS: 3/4] [shipped]
- **`dispatch_model` (preflight + resolve + transport)** — lane_health + cwd_guard preflight, tool-grant injection, session stash — `mini_ork/dispatch/providers.py:662`. [CONSENSUS: 3/4] [shipped]
- **Routing policy registry (6 policies, OCP)** — `workflow_default|frontier_only|cheap_only|static_hybrid|learning_governed|trace_governed`; `MO_ROUTING_POLICY` selects — `mini_ork/dispatch/routing.py:153` (`POLICY_REGISTRY`), `mini_ork/dispatch/routing.py:164` (`register_policy`). [CONSENSUS: 3/4] [shipped]
- **Cost circuit breaker (daily spend)** — sums 24h `task_runs.cost_usd` vs `MO_DAILY_BUDGET_USD` (default $50) → rc=42 — `mini_ork/dispatch/llm_dispatch.py:177` (`cost_circuit_open`). [CONSENSUS: 3/4] [shipped]
- **Cost-pause sentinel** — every `MO_PAUSE_EVERY_USD` (default $25) writes `.cost-pause`, blocks next call, operator-resumable — `mini_ork/dispatch/cost_pause.py:24` (`check`). [CONSENSUS: 3/4] [shipped]
- **Dispatch primitive (E2BIG-proof)** — prompt on STDIN; structured `DispatchResult`, never raises; timeout→124/OSError→127 — `mini_ork/dispatch/core.py:58`. [CONSENSUS: 2/4] [shipped]
- **`dispatch_with_fallback` (lane chain)** — first non-empty `ok` result wins; fixes one hung lane blocking a run — `mini_ork/dispatch/providers.py:1123`. [CONSENSUS: 2/4] [shipped]
- **Codex native transport + backend registry** — bespoke sidecar backend (`.tokens`/`.cost`); per-model transport swap — `mini_ork/dispatch/codex_transport.py:293` (`main`), `mini_ork/dispatch/providers.py:1109` (`MODEL_DISPATCH_BACKENDS`). [CONSENSUS: 2/4] [shipped]
- **Lane mapping (role → lane)** — canonical loop roles + heterogeneous-family lens lanes; per-recipe override — `config/agents.yaml:8`. [CONSENSUS: 2/4] [shipped]
- **Role-aware fallback chains** — coding roles → `minimax,codex,sonnet`; review → `opus,kimi,sonnet`; order-preserving dedup — `mini_ork/dispatch/routing.py:20` (`dispatch_chain`). [CONSENSUS: 2/4] [shipped]
- **Lane fuse (3-strike)** — 3 consecutive failed+retryable+same-category rows opens fuse → rc=43 — `mini_ork/dispatch/llm_dispatch.py:192` (`check_lane_fuse`). [CONSENSUS: 2/4] [shipped]
- **`llm_dispatch` wrapper (retry loop)** — `MO_DISPATCH_MAX_ATTEMPTS` (3), exp backoff+jitter capped 45s, writes `llm_calls` row, strips `<z-insight>` — `mini_ork/dispatch/llm_dispatch.py:293`. [CONSENSUS: 2/4] [shipped]
- **Throttle-guard (per-provider cool-down)** — flag-file backoff ladder `(0,300,600,1800,3600…)`; systemic halt at 3-in-600s — `mini_ork/dispatch/throttle_guard.py:100` (`record_failure`), `mini_ork/dispatch/throttle_guard.py:164` (`systemic_halt_check`). [CONSENSUS: 2/4] [shipped]
- **Native secrets store (owner-only 0600)** — atomic write, fail-closed on symlink/wrong-uid/world-readable; values never in CLI flags — `mini_ork/dispatch/secrets.py:78` (`read_secret_exports`), `mini_ork/dispatch/secrets.py:103`. [CONSENSUS: 2/4] [shipped]
- **Tool-grant hermetic dispatch** — per-node `--allowedTools` + scoped `.mcp-config.json`; implementer default `Read,Write,Edit,Bash` — `mini_ork/dispatch/providers.py:990` (`apply_tool_grants`). [CONSENSUS: 2/4] [shipped]
- **CWD guard (framework-tree refusal)** — refuses cwd inside mini-ork root unless `MO_ALLOW_FRAMEWORK_CWD=1`; fail-fast rc=2 — `mini_ork/dispatch/providers.py:684`, `mini_ork/dispatch/providers.py:633` (`cwd_guard`). [CONSENSUS: 2/4] [shipped]
- **Cache-aware cost split (telemetry)** — uncached/cached/cache-write per-Mtok breakdown; PRAGMA-gated `llm_calls` insert — `mini_ork/dispatch/providers.py:38` (`cache_aware_cost`). [CONSENSUS: 1/4] [shipped]
- **Lane-helpers (free-lane predicate + cache flags)** — frozen free set `glm,kimi,minimax`; one-shot `claude --help` capability probe — `mini_ork/dispatch/lane_helpers.py:87` (`lane_is_free`). [CONSENSUS: 1/4] [shipped]

## Runtime reliability (14 features)
- **Durable-DAG resume (E1–E5)** — resurrect a failed run at STEP (E2) or TURN (E4); checkpoint + lease + idempotency + `--resume` — `mini_ork/recovery/planner.py:91`; `db/migrations/0052_run_leases_recovery_requests.sql`; `GET /api/v1/runs/{id}/recovery`. [CONSENSUS: 4/4] [shipped]
- **Circuit breaker (liveness gate)** — trips on artifact-hash invariance or stuck reviewer verdict across last N runs; UI-visible — `mini_ork/recovery/circuit_breaker.py:120` (`check_liveness_breaker`), `mini_ork/web/routes/learning.py:526`. [CONSENSUS: 4/4] [shipped]
- **Single-writer lease + fencing (E3)** — `BEGIN IMMEDIATE` on `run_leases`, 128-bit owner token, 900s TTL — `mini_ork/stores/lease.py:112` (`acquire_lease`), `mini_ork/stores/lease.py:228` (`fence_or_reject`). [CONSENSUS: 3/4] [shipped]
- **Tool receipts + idempotency envelope (E4)** — retry-safe tool-call replay keyed on idempotency key — `mini_ork/cli/execute.py:13`; `db/migrations/0053_tool_receipts.sql`. [CONSENSUS: 3/4] [shipped]
- **Trajectory retention (TTL + gzip)** — gzips `agent-*.stream.jsonl`, prunes turn_jsonl per `MO_TRAJECTORY_TTL_DAYS`; run-scoped to avoid cross-run clobber — `mini_ork/dispatch/retention.py:56` (`gzip_run_stream`), `mini_ork/dispatch/retention.py:140` (`prune_old_trajectories`). [CONSENSUS: 3/4] [shipped]
- **Deadline budget (wall-clock cap)** — latched trip on `MO_DEADLINE_*`, writes `.deadline-hit` with best-so-far artifact path — `mini_ork/dispatch/deadline_budget.py:72` (`init`), `mini_ork/dispatch/deadline_budget.py:128` (`check`). [CONSENSUS: 3/4] [shipped]
- **Resume (cost-pause release + audit)** — clears `.cost-pause`, appends `.cost-pause-approvals.jsonl` with approver+payload — `mini_ork/cli/resume.py:70` (`resume`). [CONSENSUS: 3/4] [shipped]
- **Durable checkpoint writer + validity check (E1)** — sha256 manifest hash, INSERT OR REPLACE per `(run_id,node_id)`; `is_node_reusable` fail-closed — `mini_ork/stores/checkpoints.py:225` (`write_checkpoint`), `mini_ork/stores/checkpoints.py:330`. [CONSENSUS: 2/4] [shipped]
- **Failure-class state machine (E3)** — maps to `INFRA_INTERRUPT|PROVIDER_LIMIT|OUTPUT_INVALID|INPUT_REQUIRED|TERMINAL`; `max_turns_hit`→PROVIDER_LIMIT bounds retry — `mini_ork/learning/failure_classifier.py:102` (`classify`), `mini_ork/learning/failure_classifier.py:158`. [CONSENSUS: 2/4] [shipped]
- **Publisher-commit gate (strict-child path)** — `realpath ∈ target_repo` + `git add -- <files>` (never `-A`); `_envsubst` blanks unset `${VAR}` — `mini_ork/cli/publisher.py:33` (`_publisher_try_commit_files`). [CONSENSUS: 2/4] [shipped]
- **Rollback (workflow|agent → prev stable)** — thin over `version_registry.rollback` — `mini_ork/cli/rollback.py:1`. [CONSENSUS: 2/4] [shipped]
- **Repo integrity guard** — `check_and_heal()` before each run (cwd-confusion / core.bare recovery) — `mini_ork/cli/main.py:379`. [CONSENSUS: 1/4] [shipped]
- **`RunContext` + scoped env overrides** — typed 9-var `MINI_ORK_*` contract; `scoped_environ` restores on exit; `None`→delete key — `mini_ork/context.py:51` (`RunContext`), `mini_ork/context.py:167` (`scoped_environ`). [CONSENSUS: 1/4] [shipped]
- **File-surface lease (CAID, `--owns`)** — overlapping worktree claims refused — `mini_ork/orchestration/coord.py:1` (`cmd_acquire`/`cmd_release`/`cmd_renew`). [CONSENSUS: 1/4] [shipped]

## Verification & gates (11 features)
- **Gate registry (8 types, OCP)** — `deterministic_verifier|reviewer_gate|human_gate|budget_gate|scope_gate|deployment_gate|liveness_gate|custom`; `register_gate_evaluator` extends — `mini_ork/gates/gate_registry.py:117` (`gate_register`), `mini_ork/gates/gate_registry.py:67` (`_VALID_GATE_TYPES`). [CONSENSUS: 3/4] [shipped]
- **Promotion gate** — decision tree require_human→pending / not-all-pass→rejected / Δutility≤0→quarantined / else promoted → `promotion_records` — `mini_ork/gates/promotion_gate.py:143` (`promotion_evaluate`), `mini_ork/cli/promote.py:49`. [CONSENSUS: 3/4] [shipped]
- **Native oracle gates (in-process)** — `native:<name>` sentinels evaluated without bash spawn; `gate_bootstrap` seeds them — `mini_ork/gates/native_gates.py:1`, `mini_ork/gates/gate_registry.py:252`. [CONSENSUS: 2/4] [shipped]
- **Verifier dispatcher** — recipe verifiers + extension `.py`/`.sh` + required-artifact assertion + gate hookup — `mini_ork/cli/verify.py:130`, `mini_ork/cli/verify.py:185`. [CONSENSUS: 2/4] [shipped]
- **Per-recipe verifier scripts** — canonical typecheck+test verifier-pair topology — `recipes/code-fix/workflow.yaml:33`. [CONSENSUS: 2/4] [shipped]
- **Artifact-contract validator** — checks `outputs[]` + `success_verifiers[]` vs schema — `mini_ork/gates/artifact_contract.py:1`. [CONSENSUS: 2/4] [shipped]
- **Grounded rejection emit (evidence-cited veto)** — every gate fail cites `evidence_trace_ids` into `grounded_rejections` (anti-Goodhart audit trail) — `mini_ork/gates/common.py:111` (`emit`), `mini_ork/web/routes/learning.py:293`. [CONSENSUS: 2/4] [shipped]
- **Rubric pre-screen / grade_run_reward** — best-effort reward side-channel before verify; rubric tables — `mini_ork/cli/main.py:487`; `db/migrations/0025_verifier_rubrics.sql`. [CONSENSUS: 2/4] [shipped]
- **Citation-verifier (mechanical)** — mechanical citation-density check — `mini_ork/gates/citation_verifier_mechanical.py:1`. [CONSENSUS: 1/4] [shipped]
- **Krippendorff-α + panel-bias gates** — inter-rater agreement + reviewer-bias gates for epic-sized panels — `mini_ork/gates/krippendorff_alpha_gate.py:1`, `mini_ork/gates/panel_bias.py:1`. [CONSENSUS: 1/4] [shipped]
- **Coord-gate (advisory|strict deny)** — rc=0 advisory / rc=11 strict via `mini-ork coord gate` — `mini_ork/gates/coord_gate.py:1`. [CONSENSUS: 1/4] [shipped]

## Self-improvement & learning (17 features)
- **GRPO group-relative advantage writeback** — per `(agent_version,role,task_class)` UPSERT into `agent_performance_memory` with shrinkage(K=5) + EMA(α=0.30) + recency halflife(14d) — `mini_ork/learning/writeback.py:144` (`write_grpo_advantages`). [CONSENSUS: 3/4] [shipped]
- **Anti-Goodhart reward contract** — execution status PRIMARY, reviewer verdict VETO-only (downgrade, never fabricate positive) — `mini_ork/learning/writeback.py:32` (`reward_from_status`), `mini_ork/learning/writeback.py:23`. [CONSENSUS: 3/4] [shipped]
- **Contextual-bandit router (cost-free, UCB)** — UCB ordering default, single-sample baselines, NeuralUCB off; writes `lane_domain_advantage`/`lane_region_advantage` — `mini_ork/lane_router.py:101` (`recompute_advantages`); `db/migrations/0032_lane_relative_advantage.sql`. [CONSENSUS: 3/4] [shipped]
- **Advantage store (domain/region/slice tables)** — 3 tables + index created defensively; verbatim schema move from lane_router — `mini_ork/learning/advantage_store.py:88` (`ensure_advantage_tables`); `db/migrations/0043_lane_domain_advantage.sql`. [CONSENSUS: 3/4] [shipped]
- **Reflection pipeline (9 funcs)** — extract→dedup→link→detect-stale→summarize→suggest→persist→verify→run — `mini_ork/learning/reflection_pipeline.py:32`; `mini_ork/cli/reflect.py`. [CONSENSUS: 3/4] [shipped]
- **Textual-gradient extractor** — 5 target families (`workflow.node`, `agent.prompt`, `workflow.edge`, `verifier`, `workflow.recipe`) → `gradient_records` — `mini_ork/learning/gradient_extractor.py:23`; `db/migrations/0038_gradient_records.sql`. [CONSENSUS: 3/4] [shipped]
- **Apply loop (learn→apply closed)** — pick→materialize→score→non-regression gate→write|quarantine; master gate `MO_APPLY_ENABLED=1` — `mini_ork/cli/apply.py:96` (`apply_run`). [CONSENSUS: 3/4] [shipped] [DISPUTED anchor: minimax `apply.py:609` vs kimi `apply.py:96`]
- **Self-improve outer loop (worktree-per-iter)** — deadline-bounded; verification triple `bottlenecks→self-tests→no-regression`; converged short-circuit — `mini_ork/cli/self_improve.py:241` (`main`), `mini_ork/cli/self_improve.py:80` (`decide_outcome`). [CONSENSUS: 3/4] [shipped]
- **Semantic long-term memory (mem-a reconcile)** — cosine-class UPDATE/DELETE/ADD; `HashEmbedder` default, pluggable providers — `mini_ork/memory/semantic.py:215` (`add`), `mini_ork/memory/semantic.py:300` (`reconcile`); `db/migrations/0046_semantic_memory.sql`. [CONSENSUS: 3/4] [shipped]
- **Conductor outcome reconciliation** — joins `conductor_decisions`→`task_runs`; success = `published AND verdict≠crash` → predictions falsifiable — `mini_ork/learning/writeback.py:47` (`learning_update_conductor_outcomes`). [CONSENSUS: 2/4] [shipped]
- **Context assembler (per-node prompt surface)** — injects failure-modes + prior-runs + ContextNest capsule + steering; writes `context-pack.json` — `mini_ork/context_assembler.py:79`. [CONSENSUS: 2/4] [shipped]
- **Role-evolver (lane retire/split/rename)** — 3 signals: loser lanes (≤-0.20/≥3 runs), bug clusters, cross-class renames — `mini_ork/learning/role_evolver.py:31` (`propose`); `db/migrations/0034_topology_role_evolution.sql`. [CONSENSUS: 2/4] [shipped]
- **Process Reward Model heuristic (PRM)** — status 0.50 / reviewer 0.30 / activity 0.15 / duration 0.05; 0 on non-success — `mini_ork/learning/process_reward.py:97` (`score_trace`). [CONSENSUS: 1/4] [shipped]
- **Group-evolver (workflow candidate proposer)** — 8 mutation kinds + 3-dim novelty (nodes Jaccard + tools + failure_modes) — `mini_ork/learning/group_evolver.py:1` (`propose`). [CONSENSUS: 1/4] [shipped]
- **Utility-function scoring (6 components)** — success/verifier/quality/cost/latency/risk weighted 0.45/0.20/0.15/0.10/0.05/0.05, `MINI_ORK_W_*` overrides — `mini_ork/learning/utility_function.py:79` (`score`). [CONSENSUS: 1/4] [shipped]
- **Per-task no-regression gate (RELAI-VCL)** — blocks aggregate-up candidates that regress previously-solved held-out tasks — `mini_ork/cli/apply.py:332`. [CONSENSUS: 1/4] [shipped]
- **Topology telemetry (ρ/context/inductive distance)** — panel-shape signal → `panel_topology_telemetry` — `mini_ork/cli/topology.py:1`, `mini_ork/observability/topology_metrics.py:1`. [CONSENSUS: 1/4] [shipped]

## Observability surface (22 features)
- **FastAPI observability app (14 routers)** — `create_app` binds 127.0.0.1:7090, CORS allowlist + `null` origin for Orca — `mini_ork/web/app.py:33`. [CONSENSUS: 4/4] [shipped]
- **SPA bundle mount + API-only fallback** — serves built React when `web/dist/index.html` exists, else JSON hint — `mini_ork/web/app.py:116` (`spa_fallback`). [CONSENSUS: 4/4] [shipped]
- **SSE live event stream** — cursor over `mo_events`+`run_events`, 2s poll / 15s keepalive, sqlite via `asyncio.to_thread` (non-blocking) — `mini_ork/web/routes/stream.py:36` (`_event_loop`), `mini_ork/web/routes/stream.py:141`. [CONSENSUS: 3/4] [shipped]
- **Run control (stop/kill/pause/resume/steer)** — stop/kill loopback-trust; pause-cost/resume-cost/steer require Bearer auth — `mini_ork/web/routes/control.py:27` (`stop`), `mini_ork/web/routes/control.py:123` (`steer`). [CONSENSUS: 3/4] [shipped]
- **Token-based auth (default-deny writes)** — `Authorization: Bearer`; missing token file → all writes 401; no missing-vs-wrong leak — `mini_ork/web/auth.py:60` (`require_token`). [CONSENSUS: 3/4] [shipped]
- **Detached run launch** — non-blocking kickoff bound to operator identity — `mini_ork/web/routes/runs.py:24` (`launch`). [CONSENSUS: 3/4] [shipped]
- **Self-discovering `/api` index** — derives endpoint list from `app.routes` so index never drifts — `mini_ork/web/app.py:94` (`api_index`). [CONSENSUS: 2/4] [shipped]
- **Task-runs list + 2s summary cache** — recipe/status/verdict filters — `mini_ork/web/routes/fleet.py:119` (`list_task_runs`), `mini_ork/web/routes/fleet.py:160`. [CONSENSUS: 2/4] [shipped]
- **Run detail (11 correlated queries)** — row+agents+llm-calls+DAG+artifacts+events; trace_id→run_id→time-window bridge — `mini_ork/web/routes/run_detail.py:29`. [CONSENSUS: 2/4] [shipped]
- **Run DAG status overlay** — merges node events into recipe DAG; `never_seen|running|done|failed` — `mini_ork/web/routes/run_detail.py:592` (`get_dag`). [CONSENSUS: 2/4] [shipped]
- **Why-this-failed aggregator** — merges execute.log + verifier results + self_improve notes + traces — `mini_ork/web/routes/run_detail.py:160` (`get_why`). [CONSENSUS: 2/4] [shipped]
- **Active-runs fleet (heartbeat)** — merges legacy `runs` + modern `task_runs` — `mini_ork/web/routes/fleet.py:32` (`active_runs`). [CONSENSUS: 2/4] [shipped]
- **Learning endpoints (15 surfaces)** — bandit/gepa/failures/patterns/topology/memory/gates/reviews/conductor/… — `mini_ork/web/routes/learning.py:31`. [CONSENSUS: 2/4] [shipped]
- **Recipe fingerprint (lane/verifier topology)** — `/api/v1/fingerprint/{recipes,lanes}` — `mini_ork/web/routes/fingerprint.py:1`. [CONSENSUS: 2/4] [shipped]
- **Trajectory charts** — cost-by-day (stacked area) + wall-time + self-improve ledger — `mini_ork/web/routes/trajectory.py:116`, `mini_ork/web/routes/trajectory.py:15`. [CONSENSUS: 2/4] [shipped]
- **Recovery projection (E5)** — durable-DAG view via `recovery_admin.recovery_projection` — `mini_ork/web/routes/recovery.py:22`. [CONSENSUS: 2/4] [shipped]
- **Project registry (multi-home switcher)** — persists at `~/.config/mini-ork/projects.json`; switch never mutates DBs — `mini_ork/web/routes/projects.py:24`. [CONSENSUS: 2/4] [shipped]
- **Dispatch endpoint (Wilson-CI honesty)** — ranked lanes; `<5` samples returns `evidence:"none"` (no number) — `mini_ork/web/routes/dispatch.py:65`, `mini_ork/web/routes/dispatch.py:47`. [CONSENSUS: 2/4] [shipped]
- **OTel span buffer + Langfuse OTLP flush** — `MO_OTEL=1` gate; JSONL buffer → OTLP POST; no-space JSON byte-parity — `mini_ork/observability/otel.py:73` (`mo_otel_enabled`), `mini_ork/observability/otel.py:245` (`mo_otel_flush`). [CONSENSUS: 2/4] [shipped]
- **React UI (routes + components)** — Fleet/Trajectory/Fingerprint shell, RunDag renderer, WhyCard, AgentTranscript — `mini_ork/web/static/index.html:1`; sources `ui/src/routes/RunDetailPage.tsx`. [CONSENSUS: 1/4] [shipped]
- **Per-thread read-only WAL pool** — `threading.local` connections, `cache_size=-16000` — `mini_ork/web/db.py:38`. [CONSENSUS: 1/4] [shipped]
- **Artifact-records endpoint (path-escape reject)** — DB-registered artifact upload — `mini_ork/web/routes/artifacts.py:82`. [CONSENSUS: 1/4] [shipped]

## Operator & dev ergonomics (24 features)
- **`install` (cross-platform launcher)** — managed-marker launcher into `~/.local/bin` / `%LOCALAPPDATA%`; idempotent PATH update — `mini_ork/cli/install_command.py:13` (`main`). [CONSENSUS: 3/4] [shipped]
- **`garden` (drift detection)** — stale runs, orphan worktrees, output collisions, oversize prompts (`MAX_PROMPT_KB=32`) — `mini_ork/cli/garden.py:46`. [CONSENSUS: 3/4] [shipped]
- **`inject` (operator steering CLI)** — `--run-id/--role/--message/--severity/--confidence/--ttl-secs` → `operator_steering_messages` — `mini_ork/cli/inject.py:71` (`build_parser`). [CONSENSUS: 3/4] [shipped]
- **`recover` (durable-DAG planner CLI)** — auto-resume from STEP/TURN; pure read status — `mini_ork/recovery/planner.py:1`, `mini_ork/cli/recover.py`. [CONSENSUS: 3/4] [shipped]
- **`help`/`version`/`doctor`** — doctor preflights sqlite3/git/curl/claude/codex/python3 + provider env vars — `mini_ork/cli/main.py:597` (`_doctor_handler`), `mini_ork/cli/main.py:637`. [CONSENSUS: 2/4] [shipped]
- **`init` scaffolder** — creates `.mini-ork/{runs,config,…}` + `engine` pointer + seeds `task_classes/*.yaml` + `.gitignore` — `mini_ork/cli/init.py:224` (`mini_ork_init`). [CONSENSUS: 2/4] [shipped]
- **`serve` (UI launcher)** — preflights state.db + fastapi/uvicorn before exec; `--port/--host/--home/--reload` — `mini_ork/cli/serve.py:76` (`main`). [CONSENSUS: 2/4] [shipped]
- **`validate` (pre-run static checks)** — findings with errors/warnings + `Fix:` hints — `mini_ork/cli/validate.py:39`. [CONSENSUS: 2/4] [shipped]
- **`providers` (workflow-aware credential status/configure)** — discovers required keys from workflow lanes; reports presence, never values — `mini_ork/cli/providers.py:1`, `mini_ork/cli/providers.py:57` (`workflow_lanes`). [CONSENSUS: 2/4] [shipped]
- **`eval` (benchmark against candidate)** — reuses `learning.benchmark_suite` — `mini_ork/cli/eval.py:1` (`main`). [CONSENSUS: 2/4] [shipped]
- **`recipe-eval` (author-time recipe lint)** — static recipe evaluation — `mini_ork/cli/recipe_eval.py:1`. [CONSENSUS: 2/4] [shipped]
- **`topology` (panel-shape telemetry)** — `summary | --compute | --backfill` — `mini_ork/cli/topology.py:1`. [CONSENSUS: 2/4] [shipped]
- **`epics` (epic list/state machine)** — epic CRUD — `mini_ork/cli/epics.py:1`. [CONSENSUS: 2/4] [shipped]
- **`bugs` (sweep/list/show/prioritize/promote)** — `--promote --top N` emits a fix epic kickoff — `mini_ork/cli/bugs.py:1`. [CONSENSUS: 2/4] [shipped]
- **`spawn` (worktree-per-slug, `--owns`)** — CAID registry refuses overlapping worktrees — `mini_ork/cli/spawn.py:1`. [CONSENSUS: 2/4] [shipped]
- **`coord` (file-surface lease CLI)** — `acquire|release|renew|gate|metrics|audit` — `mini_ork/orchestration/coord.py:1`. [CONSENSUS: 2/4] [shipped]
- **`review` (pre-push harsh-critic panel)** — kimi+codex+opus panel → `pre_push_reviews` — `mini_ork/pre_push_review.py:1`, `mini_ork/cli/review.py`. [CONSENSUS: 2/4] [shipped]
- **`update` (migrations + drift report)** — engine self-update / migrator — `mini_ork/cli/update.py:1`. [CONSENSUS: 2/4] [shipped]
- **`conductor` (meta-policy picker CLI)** — per-context calibration — `mini_ork/orchestration/conductor.py:1` (`main`). [CONSENSUS: 2/4] [shipped]
- **`lifetime` (run-lifetime stats)** — per-task-class lifetime observability — `mini_ork/orchestration/lifetime.py:1`. [CONSENSUS: 2/4] [shipped]
- **`metrics` + `usage-report`** — cross-cycle rollup (JSON/Markdown) + per-(region,lane) expertise — `mini_ork/cli/metrics.py:1`, `mini_ork/observability/usage_report.py:1`. [CONSENSUS: 2/4] [shipped]
- **`improve` (single-iter self-improve)** — one iteration of the outer loop — `mini_ork/cli/improve.py:1`. [CONSENSUS: 1/4] [shipped]
- **MCP steering tool (read-side)** — steering-queue consumption over MCP — `bin/mini-ork-mcp-steering:1`. [CONSENSUS: 1/4] [shipped]
- **Per-user config layering (project > home > repo)** — provider/agent resolution order — `config/providers.yaml:8`. [CONSENSUS: 1/4] [shipped]

## Specced / roadmap (not shipped as mainline) (5 features)
- **Eval-in-run-flow: metamorphic (P2) + jury (P3)** — `type:eval` node ships and writes reward cols, but LLM judge is DEMOTED to veto-only; metamorphic + jury layers specced not shipped — `recipes/eval-judge/prompts/`; `mini_ork/cli/recipe_eval.py:1`. [specced/partial]
- **GEPA live reflective evolution at scale** — `MiniOrkGEPAAdapter` is import-clean and wired, but consumed only by a research recipe; mainline uses `apply`/`role-evolver` — `mini_ork/gepa/miniork_adapter.py:81`. [specced/partial]
- **Full `web/dist` React bundle** — repo ships the manifest entrypoint; full bundle built separately (`pnpm build`), API-only otherwise — `mini_ork/web/app.py:116`; `mini_ork/web/static/index.html:1`. [specced/partial]
- **Pre-push review panel as default merge gate** — panel CLI exists; enforcement is opt-in git-hook wiring, not default-on — `mini_ork/pre_push_review.py:1`. [specced/partial]
- **Adaptive conductor gain auto-engagement** — `_adaptive_lane_gain` fires only past `MO_CONDUCTOR_GAIN_MIN_SAMPLES`; ships cold at default 0.3 below threshold — `mini_ork/orchestration/conductor.py:37`. [specced/cold-default]

## Disputed entries
- **Watchdog maturity** — minimax: shipped orphan-process reaper — `mini_ork/orchestration/watchdog.py:1`. glm: `[STATUS: incomplete]` "scaffold only; not yet wired to circuit-breaker dispatch" — `mini_ork/cli/watchdog.py`. Verdict: module exists; dispatch wiring unproven → treat as partial.
- **`usage-report` maturity** — minimax: shipped `collect_region_expertise`+`render_json` — `mini_ork/observability/usage_report.py:1`. glm: `[STATUS: incomplete]` "recently added; surface coverage rough" — `mini_ork/cli/usage_report.py:160`.
- **`bug-collector` wiring** — minimax: shipped heuristic/llm modes — `mini_ork/observability/bug_collector.py:1`. glm: `[STATUS: incomplete]` "collector wiring rough; overlap of `cli.bugs` + `cli.bug_collector`" — `mini_ork/cli/bug_collector.py`.
- **Apply loop anchor** — minimax cites `mini_ork/cli/apply.py:609` (`apply_run`); kimi cites `mini_ork/cli/apply.py:96` (`apply_run`). Both valid entrypoints into the same close-the-loop flow; no maturity dispute.
- **GEPA maturity** — minimax: `[specced/partial]` (adapter wired, mainline uses apply/role-evolver) — `mini_ork/gepa/miniork_adapter.py:81`. glm: presents `/api/v1/learning/gepa` endpoint as shipped surface. Reconciled as specced/partial (endpoint exists, mainline evolution loop does not consume the adapter).
- **`framework-edit` verifier** — glm: `[STATUS: flagged]` "verifier never emits `verdict.json`; gate-by-reviewer-pass fallback" — `mini_ork/cli/verify.py:175`. No counter-claim from other lenses; carried into coverage gaps.

## Coverage gap report
- **No `mini-ork traces`/`llm-calls` shell surface** — `llm_calls` is web-only (`mini_ork/web/routes/run_detail.py:537`); cost-per-(lane,recipe,day) only via `/cost-by-day`. Operators must hit HTTP or sqlite3 directly. [minimax gap 1]
- **Apply-loop GEPA scorer is a stub** — `MO_APPLY_SCORER=gepa` collapses to neutral mock `"0.5 1"` on import failure — `mini_ork/cli/apply.py:296`. [minimax gap 2]
- **Durable-DAG resume UX split from `mini-ork resume`** — `resume` clears cost-pause sentinel (`mini_ork/cli/resume.py:127`); "died at node X, resume from X" lives under `recover`+`recovery.planner`, not one CLI. [minimax gap 3]
- **Conductor budget projection is shallow** — `_today_cost` checks 24h rolling spend but does not project per-epic; the `$50/day` cap is enforced per self-improve iter (`mini_ork/cli/self_improve.py:198`) not mid-run for the live scheduler — `mini_ork/orchestration/conductor.py:24`. [minimax gap 4]
- **No `process_reward` investigation surface** — written by executor (`mini_ork/cli/execute.py`), shown via conductor endpoint, but no shell "why did node X score 0.42?" — diagnostic only at `mini_ork/web/routes/run_detail.py:192`. [minimax gap 5]
- **No cross-recipe learning-portability surface** — `mini_ork/learning/cross_epic_gradient.py:1` exists but is not exposed via any `/api/v1/learning/` route; can't ask "what has the system learned about implementer prompts across all recipes?". [minimax gap 6]
- **Observability spans absent on hot paths** — no OTel/withFeature span crosses dispatch, GRPO writeback, cost-circuit, lane-fuse, or the recovery planner; cost-circuit and lane-fuse emit stderr only, with no persistent row to graph "lane was OPEN" — `mini_ork/dispatch/llm_dispatch.py:177`, `mini_ork/learning/writeback.py:144`. [codex OBS gaps: Flows 7/11/12/18/22]
- **`framework-edit` verifier never emits `verdict.json`** — always renders `needs_revision`; gate falls back to reviewer-pass — `mini_ork/cli/verify.py:175`. [glm flagged]
- **`eval-judge` metamorphic (P2) + jury (P3) specced, not shipped** — LLM judge is veto-only today — `mini_ork/cli/recipe_eval.py:1`; `recipes/eval-judge/prompts/`. [glm incomplete]
- **`watchdog` / `usage-report` / `bug-collector` partially wired** — each has a module but rough or unproven runtime wiring; see Disputed entries — `mini_ork/orchestration/watchdog.py:1`, `mini_ork/observability/usage_report.py:1`, `mini_ork/observability/bug_collector.py:1`.
