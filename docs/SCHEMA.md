# mini-ork Schema Reference

Full per-table column reference, grouped by layer. Foreign keys are noted inline.
All timestamps are ISO-8601 UTC (`strftime('%Y-%m-%dT%H:%M:%fZ','now')`) unless
the column type is `INTEGER` (Unix epoch ms, used by V2/V3 reflection tables).

---

## Core Lifecycle

### `schema_migrations`
Migration idempotency guard — one row per applied `.sql` file.

| Column | Type | Notes |
|---|---|---|
| `filename` | TEXT PK | Migration file name, e.g. `0001_core.sql` |
| `applied_at` | TEXT | ISO-8601 UTC timestamp |
| `checksum` | TEXT | Optional file checksum for tamper detection |

---

### `epics`
Top-level unit of work dispatched to a worker agent.

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | e.g. `EPIC-42` |
| `title` | TEXT | Human-readable description |
| `status` | TEXT | `not started` → `in progress` → `in review` → `done` (or `blocked`/`escalated`) |
| `lane` | TEXT | Agent lane hint, e.g. `glm`, `kimi`, `sonnet` |
| `worker_default` | TEXT | Preferred agent for worker role |
| `reviewer` | TEXT | Preferred agent for reviewer role (default: `sonnet`) |
| `group_id` | TEXT | Optional grouping tag, e.g. `bcf`, `auth-v6` |
| `kickoff_path` | TEXT | Relative path to kickoff `.md` file |
| `estimated_days` | REAL | Effort estimate |
| `notes` | TEXT | Free-form notes |
| `created_at` | TEXT | |
| `updated_at` | TEXT | Auto-updated by trigger on every UPDATE |
| `archived_at` | TEXT | Non-NULL = soft-deleted |
| `primary_journey_id` | TEXT | Logical FK → `journeys.id` |
| `epic_kind` | TEXT | `fe`/`be`/`llm`/`data`/`sandbox`/`doc`/`mixed` |
| `salvage_attempts` | INT | Incremented each time the epic is salvage-dispatched |
| `last_conflict_kind` | TEXT | Most recent merge conflict type |
| `last_conflict_at` | TEXT | Timestamp of last conflict |

**Constraints:** `done` status is irreversible (trigger); `done` epics cannot be deleted without setting `archived_at` first.

---

### `deps`
Directed epic dependency graph.

| Column | Type | Notes |
|---|---|---|
| `epic_id` | TEXT | FK → `epics.id` |
| `depends_on` | TEXT | FK → `epics.id` |
| `note` | TEXT | Optional reason |

---

### `runs`
One execution run per agent attempt on an epic. An epic may have multiple runs (retries, salvage).

| Column | Type | Notes |
|---|---|---|
| `id` | INT PK | |
| `epic_id` | TEXT | FK → `epics.id` |
| `run_dir` | TEXT UNIQUE | Relative path to run directory |
| `branch` | TEXT | Git branch for this run |
| `baseline_sha` | TEXT | `main` HEAD at worktree creation |
| `agent` | TEXT | Agent that ran this, e.g. `glm`, `kimi` |
| `brain_picked` | INT | 1 = brain advisor selected this agent |
| `started_at` | TEXT | |
| `ended_at` | TEXT | NULL while in-flight |
| `final_verdict` | TEXT | `APPROVE`/`REQUEST_CHANGES`/`ESCALATE`/`CRASH`/`SALVAGED`/`MERGED` |
| `merged_sha` | TEXT | Post-merge commit SHA on main |
| `cost_usd` | REAL | |
| `claude_session_id` | TEXT | |
| `zellij_session_name` | TEXT | |
| `last_heartbeat_at` | TEXT | Liveness probe |
| `pid` | INT | OS process ID |
| `host` | TEXT | |
| `test_status` | TEXT | `pass`/`fail`/`skip`/`error` |
| `trace_status` | TEXT | `pass`/`fail`/`skip`/`error` |
| `test_trace_id` | TEXT | OTel trace ID for the test run |

---

### `iters`
Per-iteration record within a run. Each iter = one worker pass + one reviewer verdict.

| Column | Type | Notes |
|---|---|---|
| `id` | INT PK | |
| `run_id` | INT | FK → `runs.id` |
| `n` | INT | Iteration number (1-based) |
| `verdict` | TEXT | Reviewer verdict for this iter |
| `feedback_json` | TEXT | `{issues:[{category,severity,description}]}` |
| `worker_log` | TEXT | Relative path to worker log file |
| `cost_usd` | REAL | |
| `exit_code` | INT | Worker CLI exit code |
| `started_at` / `ended_at` | TEXT | |
| `input_tokens` / `output_tokens` | INT | |
| `cache_read_tokens` / `cache_creation_tokens` | INT | |
| `web_search_requests` / `web_fetch_requests` | INT | |
| `model_provider` / `service_tier` | TEXT | |
| `duration_seconds` | INT | |
| `debugger_verdict` | TEXT | Verdict from optional debugger subagent |

---

### `inbox`
Human-escalation queue. An open inbox item blocks the epic from being re-claimed.

| Column | Type | Notes |
|---|---|---|
| `id` | INT PK | |
| `epic_id` | TEXT | FK → `epics.id` |
| `kind` | TEXT | `escalation`/`stuck`/`scope-violation`/`question`/`human-only` |
| `opened_at` | TEXT | |
| `resolved_at` | TEXT | NULL = open |
| `resolution` | TEXT | `reset-retry`/`override-done`/`halt`/`reassigned` |
| `body_md` | TEXT | Full markdown payload |
| `source_run_id` | INT | FK → `runs.id` |

---

### `locks`
Distributed mutex tokens for serialising merge, gauntlet, and plane-sync operations.

| Column | Type | Notes |
|---|---|---|
| `name` | TEXT PK | Lock name, e.g. `merge`, `gauntlet` |
| `holder` | TEXT | e.g. `orch-pid-81065` |
| `acquired_at` | TEXT | |
| `expires_at` | TEXT | TTL; stale locks may be reaped by crash recovery |

---

### `agent_profile`
Aggregated per-agent performance statistics updated by the brain advisor.

| Column | Type | Notes |
|---|---|---|
| `agent` | TEXT PK | |
| `total_runs` | INT | |
| `approval_rate` | REAL | 0..1 |
| `avg_iters_to_approve` | REAL | Among APPROVED runs only |
| `avg_cost_per_run_usd` | REAL | |
| `top_rejection_category` | TEXT | Highest-frequency feedback category (last 30d) |
| `top_rejection_count` | INT | |
| `observed_pattern` | TEXT | Free-text behavioural note |
| `updated_at` | TEXT | |

---

### `brain_decisions`
Audit log of every routing decision made by the brain advisor.

| Column | Type | Notes |
|---|---|---|
| `id` | INT PK | |
| `ts` | TEXT | |
| `trigger` | TEXT | `pick-next`/`reviewer-ambiguous`/`worker-failed`/`checkpoint`/`post-rollback`/`gauntlet-failed` |
| `lane_filter` | TEXT | Lane constraint applied at decision time |
| `claimable_json` | TEXT | Snapshot of `v_claimable` rows at decision time |
| `decision_json` | TEXT | `{epic_id, agent, rationale, parallel_safe, override_default}` |
| `cost_usd` | REAL | |
| `raw_response_path` | TEXT | Relative path to full LLM response |

---

### `subagent_runs`
Child agents spawned by a parent session (Explore subagents, voltagent, etc.).

| Column | Type | Notes |
|---|---|---|
| `id` | INT PK | |
| `parent_dispatch_id` | INT | FK → `orch_dispatches.id` |
| `parent_run_id` | INT | FK → `runs.id` |
| `parent_claude_session_id` | TEXT | |
| `child_claude_session_id` | TEXT | Set by SubagentStop hook |
| `subagent_type` | TEXT | `Explore`/`voltagent-…` |
| `description` | TEXT | |
| `prompt_excerpt` | TEXT | First 240 chars of prompt |
| `result_excerpt` | TEXT | First 480 chars of result |
| `status` | TEXT | `spawned`/`running`/`completed`/`failed`/`cancelled` |
| `started_at` / `ended_at` | TEXT | |
| `cwd` | TEXT | Working directory |
| `duration_ms` | INT | |

---

### `epic_agent_assignments`
Current agent assignment per epic. Overwritten when the brain re-assigns.

| Column | Type | Notes |
|---|---|---|
| `epic_id` | TEXT PK | May exist before `epics` row (scaffold) |
| `agent_id` | TEXT | References `config/agents/<id>.yaml` |
| `rationale` | TEXT | |
| `assigned_by` | TEXT | `human`/`brain`/`scaffold`/`fallback`/`seed-from-yaml` |
| `assigned_at` | TEXT | |

Trigger `trg_epic_agent_assignments_archive` snapshots the old row into `epic_agent_assignment_history` on every UPDATE.

---

## Mini-Orch Sessions

### `orch_dispatches`
Mini-orch dispatch records. A dispatch may fan out to child dispatches.

| Column | Type | Notes |
|---|---|---|
| `id` | INT PK | |
| `parent_dispatch_id` | INT | FK → `orch_dispatches.id` (NULL = root) |
| `epic_id` | TEXT | Not FK — mini-orch may dispatch before epics row exists |
| `group_id` | TEXT | e.g. `bcf`, `user-menu-v8` |
| `dispatched_by` | TEXT | `claude-session`/`orchestrator`/`human-cli`/`scaffold` |
| `claude_session_id` | TEXT | |
| `zellij_session_name` | TEXT | |
| `kickoff_path` | TEXT | |
| `run_dir` | TEXT | |
| `status` | TEXT | `pending`→`in_progress`→`fanned_out`→`completed`/`cancelled` |
| `rationale` | TEXT | |
| `created_at` / `updated_at` / `closed_at` | TEXT | |
| `test_status` | TEXT | `pass`/`fail`/`skip`/`pending` |

---

### `mini_orch_sessions`
LLM call cache keyed on `(epic_id, iter, stage, input_hash)`. Identical inputs reuse the cached output, saving cost. `reused_count` tracks savings.

| Column | Type | Notes |
|---|---|---|
| `uuid` | TEXT PK | |
| `job_id` | TEXT | |
| `epic_id` | TEXT | |
| `iter` | INT | |
| `stage` | TEXT | `spec-author`/`spec-reviewer`/`mutation-adversary`/`mutation-validator`/`rubric`/`worker`/`reviewer`/`bdd-runner`/`reflection-refiner` |
| `input_hash` | TEXT | |
| `status` | TEXT | `running`/`success`/`failed`/`resumable` |
| `output_path` | TEXT | |
| `log_path` | TEXT | |
| `cost_usd` | NUMERIC | |
| `turns` / `duration_ms` | INT | |
| `created_at` / `updated_at` / `expires_at` | TEXT | |
| `reused_count` | INT | Incremented on cache hit |
| `prompt_version` | TEXT | |

---

### `mo_events`
Append-only event log for every orchestrator lifecycle event. Never updated — new rows only.

| Column | Type | Notes |
|---|---|---|
| `id` | INT PK | |
| `epic_id` | TEXT | Correlation key |
| `dispatch_id` | INT | FK → `orch_dispatches.id` |
| `run_id` | INT | FK → `runs.id` |
| `iter` | INT | |
| `ts` | TEXT | |
| `duration_ms` | INT | Span duration (for start/end pairs) |
| `event_type` | TEXT | See CHECK constraint for full enum |
| `actor` | TEXT | `orch`/`kimi`/`sonnet:reviewer`/`debugger`/etc. |
| `status` | TEXT | `start`/`ok`/`fail`/`skip`/`pending` |
| `artifact_path` | TEXT | Absolute path to raw file on disk |
| `parent_event_id` | INT | FK → `mo_events.id` |
| `cost_usd` | REAL | |
| `trace_id` | TEXT | W3C trace_id for OTel correlation |
| `payload_json` | TEXT | Typed JSON blob per `event_type` |

---

### `agent_messages`
Agent-to-agent pub/sub. Messages expire after `ttl_seconds`; mediator rejects depth ≥ 3.

| Column | Type | Notes |
|---|---|---|
| `id` | INT PK | |
| `from_session` / `from_role` | TEXT | Sender identity |
| `to_session` / `to_role` / `topic` | TEXT | Addressing |
| `kind` | TEXT | `ask`/`tell`/`reply`/`heartbeat`/`subscribe` |
| `body_json` | TEXT | |
| `reply_to_id` | INT | FK → `agent_messages.id` |
| `status` | TEXT | `pending`/`delivered`/`answered`/`expired`/`failed`/`rejected` |
| `ts_created` / `ts_delivered` / `ts_answered` | TEXT | |
| `ttl_seconds` | INT | Default 300 |
| `depth` | INT | Loop-prevention counter |
| `cost_usd` | REAL | Mediator records resume cost |
| `trace_id` | TEXT | |
| `epic_id` | TEXT | ACL lineage check |
| `error_msg` | TEXT | On `failed`/`rejected` |

---

### `llm_calls`
Per-call LLM telemetry for cost tracking and OTel export.

| Column | Type | Notes |
|---|---|---|
| `id` | INT PK | |
| `provider` | TEXT | `anthropic`/`google`/`openai`/`deepseek`/`openrouter` |
| `model_id` | TEXT | |
| `tier` | TEXT | `fast`/`default`/`smart`/`pro`/`reasoning`/`embedding` |
| `feature_name` | TEXT | `mini-orch:detective`/`mini-orch:reviewer`/etc. |
| `actor` | TEXT | |
| `epic_id` / `dispatch_id` / `run_id` / `iter` | — | Correlation |
| `input_tokens` / `output_tokens` / `total_tokens` | INT | |
| `cost_usd` / `duration_ms` | REAL/INT | |
| `status` | TEXT | `success`/`failed` |
| `finish_reason` / `error_message` | TEXT | |
| `traceparent` | TEXT | W3C `version-traceid-spanid-flags` |
| `metadata_json` | TEXT | |
| `ts` | TEXT | |

---

## Tickets + Gauntlet

### `tickets`
Bug and gap tickets produced by hunter agents and the detective.

| Column | Type | Notes |
|---|---|---|
| `ticket_id` | TEXT UNIQUE | e.g. `T-0042` |
| `parent_epic_id` | TEXT | FK → `epics.id` |
| `category` | TEXT | Open text; project-defined |
| `severity` | TEXT | `blocker`/`major`/`minor` |
| `source` | TEXT | Who reported this |
| `page_route` | TEXT | |
| `file_hint` | TEXT | |
| `evidence` | TEXT | Log snippet, curl output, etc. |
| `fix_brief` | TEXT | |
| `status` | TEXT | `open`/`claimed`/`in_progress`/`done`/`wontfix`/`duplicate`/`dup-pending` |
| `fingerprint` | TEXT | sha1 of evidence signature — immutable post-insert |
| `superseded_by` | TEXT | FK → `tickets.ticket_id` |
| `attempts` / `last_attempt_at` | INT/TEXT | |
| `journey_id` | TEXT | Logical FK → `journeys.id` |

---

### `ticket_attempts`
Per-attempt log for ticket fix runs.

| Column | Type | Notes |
|---|---|---|
| `ticket_id` | TEXT | |
| `attempt_number` | INT | 1-based |
| `agent` | TEXT | |
| `outcome` | TEXT | `merged`/`gauntlet_failed`/`no_commits`/`timeout`/`api_error`/`superseded`/`cancelled`/`validator_failed`/`unknown` |
| `branch` / `run_dir` / `fixed_in_commit` | TEXT | |
| `category` / `severity` | TEXT | Echoed at dispatch time for historical queries |

---

### `detective_classifications`
Root-cause classification of a failing epic by the detective agent.

| Column | Type | Notes |
|---|---|---|
| `epic_id` | TEXT | |
| `classification` | TEXT | `baseline_rot`/`dual_types_trap`/`scope_violation`/`real_bug`/`infra_failure`/`unknown` |
| `confidence` | REAL | 0..1 |
| `source_run_id` | INT | FK → `runs.id` |
| `promoted_ticket` | TEXT | FK → `tickets.ticket_id` |

---

### `missed_by_gauntlet`
Post-mortems for bugs that reached prod despite passing the gauntlet.

| Column | Type | Notes |
|---|---|---|
| `reporter` | TEXT | `human`/`auto-probe`/`user-bug-report` |
| `epic_id` | TEXT | |
| `category` | TEXT | Open text |
| `promoted_to_gauntlet` | BOOL | True once a gauntlet step covers this case |

---

### `lessons_bank`
LLM-inferred or human-authored failure→recovery rules.

| Column | Type | Notes |
|---|---|---|
| `failure_class` | TEXT | See CHECK enum (14 classes) |
| `recovery_action` | TEXT | `cleaner-on-main`/`rebase-and-retry`/`switch-agent`/etc. |
| `success_count` / `failure_count` | INT | Adaptive effectiveness tracking |
| `retired_at` | TEXT | Non-NULL = lesson retired |

---

## Expected Features + Consensus

### `expected_features`
Canonical registry of features that MUST/SHOULD/COULD exist in the product.

| Column | Type | Notes |
|---|---|---|
| `route` | TEXT | URL path, e.g. `/dashboard` |
| `slug` | TEXT | Stable ID, e.g. `search-bar` |
| `design_source` | TEXT | Path to design artifact (nullable; project-defined) |
| `fingerprint` | TEXT | sha1(route|slug|description) — immutable post-insert |
| `tier` | TEXT | `MUST`/`SHOULD`/`COULD` |
| `status` | TEXT | `missing`/`partial`/`implemented`/`skipped`/`deprecated`/`proposed` |
| `journey_step_id` | TEXT | Logical FK → `journey_steps.id` |

---

### `proposed_epics`
PM-proposed epics queued for human review before entering the epic pipeline.

| Column | Type | Notes |
|---|---|---|
| `proposed_id` | TEXT UNIQUE | `PE-001` auto-incremented |
| `status` | TEXT | `pending_review`/`accepted`/`rejected`/`duplicate` |
| `promoted_epic_id` | TEXT | New `epics.id` when accepted |

---

## User Research

### `personas` / `jtbds` / `journeys` / `journey_steps`
User research primitives. `personas` → `jtbds` → `journeys` → `journey_steps` → `expected_features`.

| Table | Key column |
|---|---|
| `personas` | `id` TEXT PK (e.g. `P001-dev`) |
| `jtbds` | `persona_id` FK, `statement` TEXT |
| `journeys` | `jtbd_id` FK, `steps_json` JSON array |
| `journey_steps` | `journey_id` FK, `step_no` INT UNIQUE per journey, optional `route` |

---

## V2 Refactor Layers

All V2 tables use `INTEGER` epoch timestamps (not ISO-8601 text) and carry shared
**reflection columns**: `via_gate`, `reflection_at`, `reflection_sha`, `reflected_substrate`,
`reflection_status`, `reflection_last_check`, `reflection_drift_log`.

### `arch_specs`
Hoare-triple architecture specs: `precondition` (P), `postcondition` (Q), `verifier` (shell command).

### `module_plans`
Refactor candidate plans per arch spec (up to 3: max-cohesion, min-churn, balanced). PK: `(module_id, candidate_id)`.

### `atom_prs`
Atomic PR specs derived from module plans. `kind` ∈ `{rename, extract, inline, signature_change, delete, wire}`.

### `adrs`
Architecture Decision Records. Immutable once `accepted`; superseded by newer ADRs via `replaced_by`.

---

## V3 Validation Layers

### `node_annotations`
DSAP annotations on code symbols. `node_id` format: `fn:path/to/file.ts:symbolName`. `content_hash` (XXH3) is the cache key; stale when the source file changes.

### `communities`
Louvain co-mutation clusters of `node_annotations`. Scored by `mutation_density`, `recent_failure_rate`, `hub_centrality`, `coverage_gap`. Invalidated by a fix that touches shared nodes.

### `validations`
Route-level validation results. `verdict` ∈ `{pass, retry, fatal}`. `bugs_json` carries `{node, violation, evidence, fix_suggestion}` entries.

### `fixes`
Patch attempts for validation failures. `frame_check` and `functoriality_check` guard correctness.

### `cascade_invalidations`
Communities invalidated as a side-effect of a fix that mutated shared nodes.

### `inspector_runs`
Dual-inspector (opus + codex) consensus records. `agreement=0` triggers human escalation.

---

## Reflection + Basins

### `reflection_log`
Work queue for the drift-check sweeper. One row per enqueued check; `processed_at` NULL = pending.

### `decision_basins`
Attractor basins that cluster related V2/V3 decisions by shared files. `importance` decays over time.

### `emergent_patterns`
Cross-feature patterns detected across multiple basins. `suggested_meta_adr` is a draft markdown ADR.

---

## Views

| View | Returns |
|---|---|
| `v_claimable` | Epics with `dep_state='satisfied'` and no open inbox |
| `v_epic_convergence` | Per-agent avg issue count by iteration (30d) |
| `v_failure_patterns` | Top REQUEST_CHANGES categories by frequency (7d) |
| `v_agent_performance` | Per-agent pass rate, avg iters, total cost (30d) |
| `mini_orch_cache_stats` | Per-stage cache hit rate and cost savings |
| `v_last_iter` | Latest iteration verdict per epic |
| `v_spend_today` | Per-agent spend today |
| `v_tickets_open` | Open tickets summary |
| `v_epic_timeline` | mo_events joined with epics/dispatches/runs/iters |
| `v_epic_cost` | Per-epic LLM cost from mo_events |
| `v_llm_cost_by_epic_7d` | Per-epic llm_calls cost (7d) |
| `v_llm_cost_by_actor_7d` | Per-actor llm_calls cost (7d) |
| `v_agent_inbox` | Pending agent_messages |
| `v_lessons_top_recoveries_30d` | Top lesson recovery actions (30d) |
| `v_carry_over_index` | Escalated epics that haven't been retried yet |
