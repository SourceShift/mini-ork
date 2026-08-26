# OpenHands-native-surface — coverage matrix (synthesis)

Synthesized from three lens reports: `glm` (endpoint enumeration), `minimax`
(data contracts → FE surfaces), `opus` (subsystem/flow + spec critique).
One factual dispute between lenses was adjudicated against the source tree
(see Disputed §D1).

## Summary

- Backend capabilities catalogued: **52** (capability-level rows; glm's
  endpoint-level enumeration underneath is 79 endpoint rows across 16 route
  modules — glm counted 15, missing `runs.py`)
- Already covered by current spec FRs: **39** (37 UNWIRED in FE + 2 backend-missing/partial on the projection seam)
- Hard GAPS (not in spec AND not in ui — add in implementation): **12**
- Consensus 3/3: **29**  |  2/3: **12**  |  single-lens: **11**
- Capabilities with NO renderable FE home today: **49** of 52 (the 3 partials:
  compat handshake wired via probe; `ui/src/components/terminal/` exists but has
  no WS bridge to `/api/v1/pty`; `ui/src/components/conversation/` exists but
  its data source `/app-conversations` is missing)
- Backend-missing routes: **1** — `POST /api/v1/app-conversations` (FR-002
  keystone). glm's claim that `POST /api/v1/runs` is also missing is **wrong**
  (see D1); the true backend build item on the launch path is one route, not two.

Status vocabulary: `UNWIRED` = in spec, backend shipped, zero FE callers;
`HARD-GAP` = in neither spec nor ui; `BE-MISSING` = in spec, backend absent;
`COMPAT-OK` = shim endpoint already wired; `RISK` = design trap, needs a
decision not a panel.

## Coverage matrix (by subsystem)

### Projection seam & agent-server shim

| capability | endpoint (file:line) | data/transport | FE surface | in spec (FR) | in ui | status | consensus |
|-----------|----------------------|----------------|-----------|--------------|-------|--------|-----------|
| Slice-1 handshake (9 compat endpoints: `/server_info`, `/api/settings` GET/PATCH, agent-schema, conversation-schema, `/api/agent-profiles`, `/alive`, `/health`, `/ready`) | `agent_server.py:112-212` | JSON | SPA probe + settings forms | no (compat) | yes | COMPAT-OK — leave | 3/3 (glm, minimax, opus) |
| Keystone conversation projection `POST /api/v1/app-conversations` (spawn + remote-START → conv descriptor) | MISSING — router at `agent_server.py:57` has no such route | JSON descriptor | conversation view (exists, data source absent) | FR-002 | no | **BE-MISSING** — AC-002 cannot pass until it ships | 3/3 (glm §1, minimax M1, opus §2) |
| Full conversation-lifecycle projection (conversation GET/list, event-stream projection, agent-state) — "the largest single build item in the plan" | shim "does NOT yet wire conversations/events to real mini-ork runs" `agent_server.py:22-25`; FE is conversation-shaped end-to-end `ui/src/routes.ts:8-42` | SSE + JSON | conversation list/detail/panel | FR-002 (drastically understated — 3 TODO bullets) | partial (components exist) | **BE-MISSING (scope)** | 1/3 (opus §2) |
| Tool-list contract: shim reports `version: "1.39.1"` with `usable_tools: []`; probe treats non-array as "all tools available" → FE may silently enable tool affordances backend can't honor | `agent_server.py:122,127` | JSON | none (defensive contract) | no | n/a | HARD-GAP — document; consider `None` over `[]` | 2/3 (minimax M2, opus) |
| Shim settings store is process-local, lost on restart — must NOT become the home of the mini-ork Bearer token | `agent_server.py:59-69` | — | token-storage decision | no | n/a | RISK | 1/3 (opus §4) |

### Fleet

| capability | endpoint (file:line) | data/transport | FE surface | in spec (FR) | in ui | status | consensus |
|-----------|----------------------|----------------|-----------|--------------|-------|--------|-----------|
| Active runs (heartbeat `runs` ∪ live `task_runs`) | `fleet.py:32-100` | JSON | fleet dashboard | FR-100 | no | UNWIRED | 2/3 (glm, opus) |
| Paginated task-run list (filter: recipe/status/verdict) | `fleet.py:119-158` | JSON | filterable table | FR-100 | no | UNWIRED | 2/3 (glm, opus) |
| Fleet summary counts (2s TTL cache) | `fleet.py:161-190` | JSON | rollup header | FR-100 | no | UNWIRED | 2/3 (glm, opus) |
| Backend health probe (db_path + table flags) | `fleet.py:21-29` | JSON | runtime-unreachable banner + per-panel degraded states | NOT in spec | no | **HARD-GAP** (disposition disputed, see D2) | 2/3 (glm, opus) |
| Dual staleness regimes: 6h active-cutoff vs 30min run-stale | `fleet.py:18`, `run_detail.py:26` | derived flags | recency chips consistent across fleet + detail | NOT in spec | no | **HARD-GAP** (fold into FR-NEW-03) | 1/3 (opus) |

### Run observability (run detail + artifacts)

| capability | endpoint (file:line) | data/transport | FE surface | in spec (FR) | in ui | status | consensus |
|-----------|----------------------|----------------|-----------|--------------|-------|--------|-----------|
| Run row + derived `stale` flag | `run_detail.py:22-48` | JSON | run header | FR-101 | no | UNWIRED | 2/3 (glm, opus) |
| Live DAG with node statuses (`never_seen\|running\|done\|failed`, verdicts, durations, edges) | `run_detail.py:592-654` | JSON (+SSE updates) | **DagView** — graph renderer, click-to-drill, ≤3s update (AC-003); the single biggest FE-surface gap; no graph component exists in `ui/src/` | FR-101 / AC-003 | no | UNWIRED | 3/3 |
| Agent roster + per-agent detail (prompt, artifact, llm calls, spawns) | `run_detail.py:51-72` | JSON | agents table + detail tabs | FR-102 | no | UNWIRED | 3/3 |
| Merged `mo_events`+`run_events` with per-row `bridge` provenance | `run_detail.py:485-534` | JSON | per-node timeline with bridge-provenance badges | FR-102 | no | UNWIRED | 3/3 |
| Per-run LLM-call ledger (trace_id strict / time-window fallback, tagged `bridge`) | `run_detail.py:537-570` | JSON | data table with bridge chips | FR-102 | no | UNWIRED | 3/3 |
| Input documents list + read (kickoff/plan/workflow/profile/answers; content inlined) | `run_detail.py:75-150` | JSON (content-as-text) | tabbed **TextViewer** (markdown/plan/yaml/json) | FR-103 | no | UNWIRED | 3/3 |
| Filesystem artifacts list + read (body inlined utf-8-replace) | `run_detail.py:573-589` | JSON (content-as-text) | size-capped inline preview by `kind`, "download past N MB" | FR-103 | no | UNWIRED | 3/3 |
| DB-registry artifact records + raw bytes (realpath-under-run-dir guard) | `artifacts.py:31-85` (guard `:82`), raw at `artifacts.py:49` | JSON + FileResponse (binary download) | artifact-records table; branch per `kind`: download vs inline viewer | FR-103 | no | UNWIRED | 3/3 |
| Aggregated failure diagnosis "why" (execute.log + verifier JSONs + self_improve notes + traces verdicts) | `run_detail.py:160-173` | JSON aggregate | WhyPanel — source-grouped, severity-ordered (AC-004) | FR-104 | no | UNWIRED | 3/3 |
| Evidence-log reader (guarded `?path=` under `.mini-ork/`) | `run_detail.py:175-189` | JSON (parsed + raw) | log viewer opened from WhyPanel (else evidence paths dead-end) | FR-104 (via why; endpoint itself omitted from spec per opus) | no | UNWIRED | 3/3 |
| Correlation / trace-bridge health (bridge_methods, issues, remediation) | `run_detail.py:192-233` | JSON diagnostics | CorrelationPanel — callouts with severity, explains empty panels | FR-104 | no | UNWIRED | 3/3 |
| Run-scoped learning: produced gradients/patterns, self-improve records, injected candidates with `injection_points[].wired` (load-bearing field) | `run_detail.py:236-343` | JSON | 3-column learning tab + injection-points table with `wired` chips | FR-202 | no | UNWIRED | 3/3 |
| Time-window-bridged rows are approximate ("may include events from concurrent runs") — UI would confidently attribute another run's LLM spend | `run_detail.py:213-216,514,555-569` | — | "approximate" badge + link to CorrelationPanel | NOT in spec | no | **HARD-GAP** | 2/3 (minimax, opus) |
| Run lifecycle state machine (`executing/verifying/reviewing/published/rolled_back/failed` + `stale`) gating which controls are valid | `run_detail.py:22`, `fleet.py:17` | — | state-conditional rendering of stop/kill/steer/pause | NOT in spec (error table handles 409s reactively) | no | **HARD-GAP** | 1/3 (opus) |
| Live cost surfacing: `cost_usd` on every `mo_events` stream row + pause-threshold sentinel | `stream.py:76`, `control.py:80-101` | SSE field | live cost ribbon vs threshold — cost governance is a headline claim with control-only surface today | NOT in spec | no | **HARD-GAP** | 1/3 (opus) |

### Streaming transports (SSE + PTY)

| capability | endpoint (file:line) | data/transport | FE surface | in spec (FR) | in ui | status | consensus |
|-----------|----------------------|----------------|-----------|--------------|-------|--------|-----------|
| Live event tail: `event: hello` then unbounded `event: event` frames (mo_events + run_events rows), 2s poll, 15s keepalive; cursors start at `MAX(id)` — post-connect events only | `stream.py:142-155` (cursor init `:45-53`, hello `:63`) | SSE | `useLiveTaskStream(runId)` hook + coalesced timeline; NOT the upstream conversation-events panel (schema mismatch — raw rows with payload_json, not OpenHands agent-server events); requires snapshot-then-subscribe + refetch-on-reconnect; fleet needs the unfiltered stream (spec only names `?task_run=`) | FR-105 (contract under-specified) | no | UNWIRED + spec amendment | 3/3 |
| PTY bridge: WS ↔ forkpty, `'0'+key` / `'1'+{rows,cols}` frames, gated `MO_PTY_ENABLED` (4403 close when off); attaches a persistent per-(project,harness) tmux session — an **orchestrator attach, not a per-run terminal** (FE must not advertise terminal-per-run) | `pty.py:107-138` (gate `:54-56`) | WebSocket/PTY binary | xterm.js bridge — `ui/src/components/terminal/` exists but no WS wiring to `/api/v1/pty`; disabled-state fallback (AC-008) | FR-106 (under-specified re per-project semantics) | partial (component unwired) | UNWIRED + spec amendment | 3/3 (glm flag, minimax M3, opus) |

### Dispatch & control

| capability | endpoint (file:line) | data/transport | FE surface | in spec (FR) | in ui | status | consensus |
|-----------|----------------------|----------------|-----------|--------------|-------|--------|-----------|
| Dispatch options: recipes + lanes + topologies with measured rates, Wilson CI, `evidence:"none"` below `MIN_SAMPLES=5` | `dispatch.py:66-188` (`:48,136-140`) | JSON | DispatchComposer comparator — CI bar + `evidence` chip are load-bearing UX; never bare rate/predicted_score | FR-300 | no | UNWIRED | 3/3 |
| `POST /dispatch` — mints run_id, records proposed-vs-chosen into `conductor_decisions` (migration 0050); explicitly does NOT spawn | `dispatch.py:225-310` (no-spawn `:230-238`, run_id `:247`, decision cols `:267-294`) | JSON | composer submit step | FR-301 (partial) | no | UNWIRED | 3/3 |
| `POST /api/v1/runs` — detached Bearer-token launch, fail-closed, accepts optional caller-supplied `run_id` | `runs.py:24-37` (optional run_id `:28`) | JSON | launch action after compose | FR-301 | no | UNWIRED — **backend EXISTS** (see D1) | 3/3 (glm found-as-missing, minimax M1 mention, opus anchored) |
| Dispatch→launch run_id threading: minted id must flow into the launch call or every `conductor_decisions.task_run_id` points at a run that never exists and labelled-example capture (the module's stated purpose, `dispatch.py:9-16`) silently breaks | `dispatch.py:247` → `runs.py:28` | flow wiring | thread id + display derived `decided_by`/`overrode` (`dispatch.py:253-257`) | NOT in spec | no | **HARD-GAP** | 1/3 (opus §2) |
| Stop / kill (SIGTERM / SIGKILL, loopback-trust by design) | `control.py:27-48` (trust posture `:9-11`) | JSON | run controls, state-gated | FR-305 | no | UNWIRED | 2/3 (glm, opus) |
| Planner Q&A (HITL): poll `/profile` for `human_questions`, POST `/answers` (unauthenticated); a polling flow, not an event flow — the one flow that most *feels* like chat; seam decision belongs in P0 | `control.py:54-74` | JSON polling | PlannerQAPanel form (typed inputs + post-answer next-step CLI) — or synthesized question events if chat renders it | FR-302 | no | UNWIRED + seam decision | 3/3 |
| Cost pause / resume (sentinel touch/clear + audit, Bearer) | `control.py:80-117` | JSON | cost-pause control + threshold state | FR-304 | no | UNWIRED | 2/3 (glm, opus) |
| Steering with `role_target`, `severity`, `confidence`, `ttl_secs` (Bearer) — steer ≠ chat message; if chat-input and native form both exist, define which is authoritative (double-injection risk) | `control.py:123-152` | JSON | full steering form, not a chat box | FR-303 | no | UNWIRED | 2/3 (glm, opus) |
| Auth posture is internally contradictory: security NFR says all writes Bearer, but `/stop`/`/kill` loopback-trust, `/answers` unauthenticated, all project writes unauthenticated; "additive only" rule forbids server-side fix | `control.py:9-11,65-74`, `projects.py:152-177` | — | per-endpoint auth table + token UX gating (AC-006/AC-007 currently assume a uniform regime the backend doesn't implement) | NOT in spec (NFR contradicted) | no | **HARD-GAP** | 1/3 (opus; glm's §7 header corroborates the Bearer subset) |

### Learning loop

| capability | endpoint (file:line) | data/transport | FE surface | in spec (FR) | in ui | status | consensus |
|-----------|----------------------|----------------|-----------|--------------|-------|--------|-----------|
| 14 learning sub-panels: bandit, gepa, failures, patterns, topology, memory, gates, reviews, conductor, workflows, benchmarks, research, epic-dependencies, circuit-breakers | `learning.py:32,78,128,160,180,223,260,312,368,409,447,483,506,525` | JSON per panel | 14 panels with explicit empty states (AC-005): tables (most), quadrant scatter for `/topology` telemetry (`rho, context_distance, inductive_distance, quadrant`), diff view for `/workflows.mutations`, citation list for `/gates` | FR-201 | no | UNWIRED | 3/3 |
| One-call `/summary` panel-header counts | `learning.py:543` | JSON | Console section header — headline should be the *loop closing*: proposals vs promotions (`learning.py:79-87`), predicted vs realized (`:368-374`) | FR-200 | no | UNWIRED | 2/3 (glm, opus) |
| Circuit breakers explain why runs silently aren't happening | `learning.py:525-527` | JSON | open-breaker callout surfaced prominently, not buried in panel 14 | FR-201 | no | UNWIRED (emphasis) | 2/3 (glm, opus) |
| `/gates` `evidence_trace_ids` + `grounded_rejections` — "the closest thing in the db to a correctness audit trail" — must be clickable through to the originating run | `learning.py:260` | JSON | clickable citation links | FR-201 (affordance unstated) | no | PARTIAL | 1/3 (minimax) |
| Conductor calibration: decisions with `outcome='pending'` and eventual `realized_score` — the loop the dispatch endpoint exists to feed | `dispatch.py:276`, `learning.py:379-389` | JSON | calibration view in conductor panel | NOT in spec | no | **HARD-GAP** | 1/3 (opus) |

Endpoint-count reconciliation: glm's table has 15 rows, minimax says "×14",
opus says "15, not 14" — no conflict: 14 sub-panels + `/summary` = 15 routes.
FR-201 covers the 14; FR-200 covers `/summary`. Checklist-slip risk noted.

### Trajectory & distillation

| capability | endpoint (file:line) | data/transport | FE surface | in spec (FR) | in ui | status | consensus |
|-----------|----------------------|----------------|-----------|--------------|-------|--------|-----------|
| Self-improve run history + detail (typed `parsed_notes` kind ∈ {flag,kv,sha}, children, siblings, linked task_run) | `trajectory.py:15-113` | JSON | timeline + detail page with typed-note badges + cross-link | FR-401 | no | UNWIRED | 3/3 |
| Cost-by-day + wall-time analytics | `trajectory.py:116-160` | JSON | stacked-area + line charts — **no chart component exists in `ui/src/` today**; lib choice open (recharts vs OpenHands-bundled) | FR-400 | no | UNWIRED (blocked on chart-lib decision) | 3/3 |
| Gradient proposal stream | `trajectory.py:163-175` | JSON | table with confidence badges | FR-400 | no | UNWIRED | 3/3 |
| TraceOtter distillation funnel (episodes → should_imitate → sft_examples) with first-class `available:false` | `traceotter.py:45-111` (`:56-57`) | JSON | funnel viz + empty-state (AC-009); load-bearing number is should-imitate, not episode count | FR-402 / AC-009 | no | UNWIRED | 3/3 |
| Mined skills + episodes with `imitate_only` training-set filter (heavy fields stripped) | `traceotter.py:113,124-166` | JSON | skill card grid + paginated episode table; the `imitate_only` toggle is the primary affordance (raw traces vs training set) | FR-402 | no | UNWIRED | 3/3 |

### Projects / idea-tree

| capability | endpoint (file:line) | data/transport | FE surface | in spec (FR) | in ui | status | consensus |
|-----------|----------------------|----------------|-----------|--------------|-------|--------|-----------|
| Projects registry: list, browse (folder walker), validate (as-you-type dry-run), add/switch/remove (refuses removing active) | `projects.py:72,86,140,152,159,167` | JSON | project switcher in shell chrome (not a buried panel) + folder browser + inline validate + destructive confirm (AC-006) | FR-404 (**mis-phased at P4**) | no | UNWIRED | 3/3 |
| Switch mutates *which state.db every other panel reads* — must invalidate all native query caches and re-home SSE subscriptions | `projects.py:159-164` | — | cross-cutting cache/SSE invalidation | NOT in spec | no | **HARD-GAP** | 1/3 (opus §2) |
| Idea-tree explorer: roots + subtree counts, node + parent/children, ancestor chain, full subtree | `idea_tree.py:29,35,47,56-70` | JSON | collapsible tree (react-arborist-class) + breadcrumbs | FR-403 | no | UNWIRED | 3/3 |

### Recovery

| capability | endpoint (file:line) | data/transport | FE surface | in spec (FR) | in ui | status | consensus |
|-----------|----------------------|----------------|-----------|--------------|-------|--------|-----------|
| Durable-DAG recovery projection: ONE merged DAG (checkpoints, attempts with exit_reason, recovery_requests, leases, next_action) "rather than a fresh unrelated run" | `recovery.py:22-26` (design `:1-8`) | JSON | recovery overlay on DagView — attempts as collapsed stacked sub-DAGs; **no conversation analogue** (a resumed run projected as chat = confusing fresh thread); belongs natively in `/console/runs/:id` | FR-107 | no | UNWIRED | 3/3 |

### Fingerprint

| capability | endpoint (file:line) | data/transport | FE surface | in spec (FR) | in ui | status | consensus |
|-----------|----------------------|----------------|-----------|--------------|-------|--------|-----------|
| Recipe list, lane map, per-recipe family fingerprint (nodes with lane/dispatch_mode/gates/verifier_ref) | `fingerprint.py:21,26,31-39` | JSON | fingerprint viewer reusing DagView + lanes sidebar | FR-403 | no | UNWIRED | 3/3 |
| Family/lane attribution is ALREADY merged into run-DAG node rows — a run-detail (P1) concern, not only the P4 viewer | `run_detail.py:610-623` | JSON (merged) | lane/family chips on DAG nodes in Phase 1 | NOT in spec (orphaned in P4) | no | **HARD-GAP** | 1/3 (opus) |

## Missing functional requirements (to fold into the spec)

- **FR-NEW-01** — Health-driven degradation — When the Console shell is mounted,
  the frontend shall poll `GET /api/v1/health` (`fleet.py:21`) and drive the
  runtime-unreachable banner and per-panel degraded states from it.
  (source: opus §3.1; glm catalogued the endpoint as "not in scope" — see D2; phase: P0)
- **FR-NEW-02** — Project switcher in shell chrome — When the user switches the
  active project (`GET /api/v1/projects`, `POST /projects/switch`,
  `projects.py:72,159`), the frontend shall invalidate all native query caches
  and re-establish SSE subscriptions, since the switch changes which state.db
  every panel reads (`projects.py:159-164`). Read+switch move to P0/P1;
  browse/add/remove stay P4. (source: opus §3.2 + §2; phase: P0)
- **FR-NEW-03** — Run lifecycle state machine — When rendering run controls, the
  frontend shall implement the status machine
  (`executing/verifying/reviewing/published/rolled_back/failed` + `stale`,
  `run_detail.py:22-48`, plus both staleness regimes `fleet.py:18`/`run_detail.py:26`)
  and render stop/kill/steer/pause affordances only in valid states.
  (source: opus §3.3 + §2; phase: P1)
- **FR-NEW-04** — Evidence resolution — When the WhyPanel references evidence
  paths, they shall resolve through `GET /task-runs/{id}/evidence?path=`
  (`run_detail.py:175-189`) with 403/404 handling. (source: opus §3.4 /
  minimax evidence row; phase: P1)
- **FR-NEW-05** — Bridge-provenance honesty — When an event or LLM-call row's
  `bridge` is `time-window`, the frontend shall visually mark it approximate
  with a link to the correlation panel (`run_detail.py:213-216,514,555`) —
  otherwise the UI confidently attributes another run's LLM spend.
  (source: opus §3.5 + minimax `/events` row; phase: P1)
- **FR-NEW-06** — Snapshot-then-subscribe — When opening any live view, the
  frontend shall REST-fetch a snapshot then subscribe to SSE from `hello`
  (`stream.py:63`), and refetch on reconnect, because the stream cursor starts
  at `MAX(id)` and emits only post-connect events (`stream.py:45-53`). The
  fleet view shall use the unfiltered stream (spec currently names only
  `?task_run=`). (source: opus §3.6 + minimax stream row; phase: P1)
- **FR-NEW-07** — Live cost ribbon — When a run is active, run detail shall
  accumulate `mo_events.cost_usd` (`stream.py:76`) into a live spend ribbon and
  show the pause-threshold state when a cost-pause sentinel exists
  (`control.py:80-101`). (source: opus §3.7; phase: P1)
- **FR-NEW-08** — Dispatch run_id threading — When launching after compose, the
  frontend shall thread the `/dispatch`-minted `run_id` (`dispatch.py:247`)
  into the launch call (`runs.py:28` / app-conversations) so
  `conductor_decisions.task_run_id` joins, and display the derived
  `decided_by`/`overrode` (`dispatch.py:253-257`). (source: opus §3.8 + §2;
  phase: P3)
- **FR-NEW-09** — Evidence-honesty rendering — When a lane/topology has
  `evidence:"none"` (n<`MIN_SAMPLES=5`, `dispatch.py:136-140,183-187`), the
  composer shall render "insufficient evidence (n<5)" and never a rate or bar.
  (source: opus §3.9 + minimax options row; phase: P3)
- **FR-NEW-10** — Conductor calibration — When rendering the conductor panel,
  the frontend shall surface decisions with `outcome='pending'` and their
  eventual `realized_score` (`dispatch.py:276`, `learning.py:379-389`).
  (source: opus §3.10; phase: P3)
- **FR-NEW-11** — Per-endpoint auth table — The spec shall carry an explicit
  auth table (Bearer: `/runs`, `/steer`, `/pause-cost`, `/resume-cost`;
  loopback: `/stop`, `/kill`, `/answers`, project writes — `control.py:9-11`,
  `projects.py:152`) and the frontend shall gate its token UX accordingly;
  AC-006/AC-007's uniform-token assumption is false and "additive only" forbids
  a server-side fix. (source: opus §3.11; glm §7 corroborates the Bearer
  subset; phase: P0 spec change)
- **FR-NEW-12** — Family attribution in P1 — When rendering the run DAG, lane/
  family attribution (already merged at `run_detail.py:610-623`) shall render
  on nodes in Phase 1; the Phase-4 fingerprint viewer covers cross-recipe
  browsing only. (source: opus §3.12; phase: P1)
- **FR-NEW-13** — Tool-list contract — The spec shall document the shim
  `usable_tools` contract (`agent_server.py:122,127`): empty array makes the
  probe's `isAgentServerToolAvailable` return false-for-all; consider `None`
  (→ "unknown") so the FE doesn't silently enable un-honorable tool
  affordances. (source: minimax M2, corroborated opus §1; phase: P0)
- **FR-NEW-14** — PTY semantics amendment — FR-106 shall state that
  `/api/v1/pty` is a per-(project,harness) orchestrator attach over a
  persistent tmux session (`pty.py:107-138`), NOT a per-run terminal; the FE
  shall not advertise terminal-per-run, and shall render the AC-008
  disabled-state on the 4403 `MO_PTY_ENABLED` close. (source: minimax M3 +
  opus §1 + glm flag; phase: P1)
- **FR-NEW-15** — Projection-seam re-scope — FR-002 shall be expanded from one
  POST into the full conversation-lifecycle projection (conversation GET/list,
  event-stream projection, agent-state), since the shim has no conversation
  lifecycle at all (`agent_server.py:22-25`) and the FE is entirely
  conversation-shaped (`ui/src/routes.ts:8-42`). Largest single build item.
  (source: opus §2; phase: P0)
- **FR-NEW-16** — Planner-QA seam decision — The spec shall decide in Phase 0
  whether planner Q&A (polled via `/profile`, `control.py:54-63`) is projected
  into chat (requires synthesizing question events) or stays a native panel —
  it is the single flow that most feels like chat. (source: opus §4; phase: P0)

Shared-primitive implementation notes (minimax, not FRs): (1) **DagView** —
one graph renderer reused by FR-101 run DAG, FR-107 recovery overlay, FR-403
fingerprint viewer, FR-NEW-12 chips; biggest single component gap. (2)
**TextViewer** — markdown/yaml/json/log renderer with size cap, reused by
`/inputs`, `/artifacts`, `/evidence`, `/why`. (3) **Chart lib decision**
(recharts vs OpenHands-bundled) blocks FR-400 charts and the FR-201
`/topology` quadrant scatter — no chart component exists in `ui/src/`.

## Disputed / needs-decision

- **D1 [DISPUTED: glm vs opus — RESOLVED for opus]** — `POST /api/v1/runs`.
  glm: "Bearer-token launch (the spec separates this from `/dispatch`) —
  **MISSING** … GAP (backend missing)" and counts "Spec-covered but BACKEND
  MISSING: 2". opus: "`runs.py:24-37` is the detached launcher (Bearer,
  fail-closed, accepts an optional caller-supplied `run_id`, `runs.py:28`)".
  Adjudicated against the tree: `mini_ork/web/routes/runs.py:24-37` defines
  `POST /api/v1/runs` (router prefix `runs.py:21`) exactly as opus describes.
  glm's module sweep covered 15 route modules and missed `runs.py`; all glm
  totals derived from that premise are off by one. Backend-missing count is
  **1** (`/app-conversations` only).
- **D2 [DISPUTED: glm vs opus]** — `GET /api/v1/health` disposition. glm:
  "Internal-only fleet health, no UI need … LEAVE (loopback diagnostics
  only)". opus: the "runtime unreachable" banner needs a probe target
  (FR-NEW-01). Recommendation: adopt opus — every native panel needs a
  degraded-state source; cost of exposure is nil.
- **D3 (reconciled, not disputed)** — learning endpoint count: glm 15 rows,
  minimax "×14", opus "15, not 14". All correct at different granularity:
  14 sub-panels (FR-201) + `/summary` (FR-200) = 15 routes. Spec checklists
  should count 15 to avoid the slip opus warns about.
- **D4 (needs-decision)** — PTY run-scoped attach: FR-106's wording implies
  per-run terminals; backend semantics are per-project tmux attach with
  `run_id` only as a cwd hint under `_is_safe_run_id` (`pty.py:107`). Decide:
  single project terminal tab (recommended, per minimax M3/opus) vs re-attach
  UX keyed on run_dir cwd.
- **D5 (needs-decision, opus §4)** — Runs are DAGs; conversations are lines.
  Flattening parallel fan-out nodes into one chat timeline misrepresents
  causality and buries per-node failure. Decide the split: DAG panel
  (`run_detail.py:592-654`) is canonical for structure; conversation view
  carries only the linearizable surface (narrative events, terminal) with
  bidirectional deep links; no per-node chat threads.
- **D6 (needs-decision, opus §4)** — Steer authority: if both a chat input and
  the native steering form can inject (`control.py:123-136`), define which is
  authoritative to avoid double-injection.
- **D7 (risk, opus §4)** — Bearer-token storage must not resolve to the shim's
  process-local settings store (`agent_server.py:59-69`) or tokens vanish on
  every server restart.

## Phase assignment recommendation

- **P0 (foundation + seam):** FR-NEW-01 (health), FR-NEW-02 (project
  read+switch — **pulled forward from P4**, where the current phasing orphans a
  capability that mutates the data source under every P1–P3 panel), FR-NEW-11
  (auth table), FR-NEW-13 (tool-list contract), FR-NEW-15 (projection-seam
  re-scope), FR-NEW-16 (QA seam decision). Backend build: the ONE missing
  route, `POST /api/v1/app-conversations` (+ lifecycle projection per
  FR-NEW-15) — 3/3 consensus keystone blocker for AC-002.
- **P1 (observability):** FR-NEW-03, FR-NEW-04, FR-NEW-05, FR-NEW-06,
  FR-NEW-07, FR-NEW-12, FR-NEW-14; build order gated by the two shared
  primitives — DagView first (FR-101/AC-003 centerpiece, reused ×4), then
  TextViewer (reused ×4). Recovery (FR-107) lands here as a DagView overlay,
  not a separate page.
- **P2/P3 (learning + dispatch):** FR-NEW-08, FR-NEW-09, FR-NEW-10. The
  chart-lib decision must be made before the FR-201 `/topology` quadrant
  scatter — flagging that the current phasing implicitly orphans it until the
  P4 trajectory work if the lib choice waits.
- **P4 (long tail):** projects browse/add/remove (rest of FR-404), fingerprint
  cross-recipe viewer (FR-403 — node-level attribution already moved to P1 via
  FR-NEW-12), idea-tree explorer, trajectory charts (FR-400 — blocked on the
  same chart-lib decision; make it in P2/P3).
- **Orphan check:** the current phasing orphans (a) project switching in P4
  (fixed by FR-NEW-02), (b) family attribution in the P4 viewer (fixed by
  FR-NEW-12), (c) the `/api/v1/health` probe entirely (fixed by FR-NEW-01),
  (d) the chart-dependent FR-201 topology panel if the lib decision slips
  past P2. No catalogued capability remains unassigned after these four moves.
