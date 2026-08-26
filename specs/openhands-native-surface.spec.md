# Feature: Expose mini-ork's Native Surface in the OpenHands Agent-Canvas FE

> **Coverage-panel amendments folded in 2026-08-26.** A 3-lens mini-ork audit
> (`specs/openhands-native-surface.coverage.md`, run `ui-parity-1787735868`)
> catalogued 52 backend capabilities across 16 route modules and found 16
> missing FRs + one FR-002 rescope. Those are folded into this spec (see
> "## Coverage-Panel Amendments" below and the inline FR-002 rescope). The
> coverage doc remains the evidence/audit trail; this spec is the authoritative
> build contract.

## Overview

The OpenHands agent-canvas SPA (`ui/`) currently speaks **only** the OpenHands
agent-server wire protocol (conversations, events, settings, files, git,
profiles, bash, automation). mini-ork's ~60 native `/api/v1/*` endpoints — run
dispatch, live DAG observability, the 14-endpoint learning loop, trajectory
analytics, TraceOtter distillation, HITL run control — have **zero** callers in
the FE today. This feature makes the full mini-ork surface reachable from the
existing SPA using a **hybrid** integration: live runs flow through the
conversation view via a run→conversation projection seam, while capabilities
that have no conversation analogue (learning loop, fleet, trajectory) get
dedicated native "Console" panels. Delivery is **phased** across the full
surface (observability first in each area, then controls).

**User value.** An operator gets one cohesive app to launch runs, watch the DAG
execute live, drill into any agent/LLM-call, diagnose failures, steer or halt a
run mid-flight, and inspect the self-improvement machinery (bandit policy, GEPA
promotions, circuit breakers) that makes mini-ork a *learning* orchestrator
rather than a plain agent runner — instead of curling `/api/v1/*` by hand.

## Architecture Decision (locked)

- **Hybrid.** Two integration seams:
  1. **Projection seam** — the backend projects a mini-ork run into the
     OpenHands conversation contract so the live experience (chat/event stream,
     xterm terminal, file/diff) is reused unchanged. Keystone endpoint:
     `POST /api/v1/app-conversations` (spawn run + remote START).
  2. **Native Console seam** — a new top-level section in the SPA with panels
     that call `/api/v1/*` directly for data with no chat analogue
     (`/console/fleet`, `/console/runs/:id`, `/console/learning`,
     `/console/trajectory`, `/console/dispatch`).
- **Scope:** full surface, phased. Every phase ships read views before its
  write/control actions.
- **Backend contract:** additive only. No changes to existing `/api/v1/*`
  semantics; the FE learns to consume them and the shim grows the projection
  endpoints.

## Phasing Map

| Phase | Cluster | Seam | Risk |
|-------|---------|------|------|
| **0** | Foundation: native API client, Console nav shell, projection seam | both | med (backend) |
| **1** | Run observability (fleet, run detail, DAG, events, LLM calls, why, SSE, PTY) | native + projection | low |
| **2** | Learning loop (14 endpoints) | native | low (read-only) |
| **3** | Dispatch & control (composer + launch; stop/kill/steer/cost-pause/answers) | native + projection | **high (writes)** |
| **4** | Trajectory & distillation (trajectory, self-improve, gradients, TraceOtter, idea-tree, recovery) | native | low |

**Amendments (2026-08-26).** P0 additionally carries: health-driven degradation
(FR-NEW-01), project read+switch pulled forward from P4 (FR-NEW-02 — it mutates
the data source under every P1–P3 panel), the per-endpoint auth table (FR-NEW-11),
the tool-list contract (FR-NEW-13), the FR-002 lifecycle rescope (FR-NEW-15), and
the planner-QA seam decision (FR-NEW-16). See "## Coverage-Panel Amendments".

---

## Functional Requirements

### Phase 0 — Foundation

#### FR-000: Native API client
The system shall provide a typed FE API client for the mini-ork `/api/v1/*`
surface, distinct from `@openhands/typescript-client`, targeting the runtime on
`127.0.0.1:7090` (via the existing Vite dev proxy in dev, same-origin in prod).

#### FR-001: Console navigation shell
Where the mini-ork native Console is enabled, the system shall render a
top-level "Console" nav entry exposing sub-routes `/console/fleet`,
`/console/runs`, `/console/learning`, `/console/trajectory`, `/console/dispatch`
alongside the unchanged OpenHands conversation UI.

#### FR-002: Run→conversation projection (spawn + full lifecycle) — RESCOPED
When a user creates a conversation whose target is a mini-ork run, the backend
shall accept `POST /api/v1/app-conversations`, mint a `run_id`, spawn the run
detached, remote-START it, and return a conversation descriptor consumable by
the existing OpenHands conversation view.

**Rescope (FR-NEW-15, 3/3 consensus).** FR-002 is NOT one POST — it is the full
conversation-lifecycle projection and the single largest build item in the plan.
The compat shim has **no conversation lifecycle at all** (`agent_server.py:22-25`
states it "does NOT yet wire conversations/events to real mini-ork runs") while
the FE is conversation-shaped end-to-end (`ui/src/routes.ts:8-42`). The backend
shall therefore additionally project:
- conversation GET/list/search (`GET /api/v1/app-conversations[/search]`,
  `GET /api/v1/app-conversations/{id}/start-tasks` mapping run-state →
  ExecutionStatus/SandboxStatus),
- the event-stream projection (`GET /api/conversations/{id}/events[/search]` +
  websocket) translating mini-ork `mo_events` rows → OpenHands `oh_event` shape
  (schema mismatch is real: raw rows carry `payload_json`, not agent-server
  events — reuse `stream.py` tail as the source, translate at the seam),
- agent-state for the conversation.
`POST /api/v1/app-conversations` is **backend-missing** (confirmed: the router at
`agent_server.py:57` has no such route) — AC-002 cannot pass until it ships. It
is the 3/3-consensus keystone: spawn-run-server-side = remote-START +
Orca-launcher-replacement + canvas-functional in one move.

#### FR-003: Feature flag
Where `VITE_MINIORK_CONSOLE` is unset or false, the system shall hide the
Console section and behave exactly as the upstream OpenHands SPA (safe rollback).

### Phase 1 — Run Observability

#### FR-100: Fleet view
When the user opens `/console/fleet`, the system shall display active runs
(`GET /api/v1/runs/active`), the paginated task-run list
(`GET /api/v1/task-runs`, filterable by recipe/status/verdict), and the
cost/status rollup header (`GET /api/v1/task-runs/summary`).

#### FR-101: Run detail with live DAG
When the user opens `/console/runs/:id`, the system shall render the recipe DAG
with per-node live status (`GET /api/v1/task-runs/{id}/dag`), the run row with
staleness flag (`GET /api/v1/task-runs/{id}`), and update node status in near
real time from the event stream.

#### FR-102: Event, LLM-call, and agent drilldown
While viewing a run, when the user selects a node or opens the inspector, the
system shall show correlated events (`/events`), LLM calls (`/llm-calls`), the
per-run agent roster (`/agents`), and full per-agent detail — prompt, output
artifact, child spawns — (`/agents/{node_id}`).

#### FR-103: Inputs & artifacts browser
While viewing a run, the system shall list and render source documents
(`/inputs`, `/inputs/{key}`), filesystem artifacts (`/artifacts`,
`/artifacts/{relpath}`), and DB-registry artifacts (`/artifact-records`,
`/artifact-records/{id}/raw`).

#### FR-104: Failure diagnosis ("why") & correlation health
When a run has failed or panels are empty, the system shall surface the
aggregated failure diagnosis (`/why`) and the trace-bridge correlation health
(`/correlation`) explaining why data may be missing.

#### FR-105: Live event stream
While a run is active, the system shall subscribe to
`GET /api/v1/stream?task_run={id}` (SSE) and reflect new `mo_events` /
`run_events` in the run detail and fleet views without a manual refresh.

#### FR-106: Live terminal (PTY)
Where `MO_PTY_ENABLED=1`, when the user opens the terminal panel for a run, the
system shall bridge the OpenHands xterm to `WS /api/v1/pty` (params
`run_id`, `cmd`, `cols`, `rows`) against the run's tmux session.

#### FR-107: Recovery projection
When a run has failed and is resumable, the system shall display the durable-DAG
recovery projection (`GET /api/v1/runs/{run_id}/recovery`) — node checkpoints,
attempts, recovery requests, leases.

### Phase 2 — Learning Loop

#### FR-200: Learning summary header
When the user opens `/console/learning`, the system shall display the one-call
summary counts (`GET /api/v1/learning/summary`): bandit arms, gradients,
promotions, failures, circuit breakers.

#### FR-201: Learning sub-panels
Where the learning section is active, the system shall provide navigable panels
for each learning endpoint: bandit policy (`/bandit`), GEPA prompt evolution
(`/gepa`), failure memory (`/failures`), emergent patterns (`/patterns`),
topology telemetry (`/topology`), task/agent memory (`/memory`), veto gates
(`/gates`), pre-push reviews (`/reviews`), conductor calibration
(`/conductor`), workflow self-modification (`/workflows`), benchmark gates
(`/benchmarks`), arXiv research (`/research`), epic dependencies
(`/epic-dependencies`), and circuit breakers (`/circuit-breakers`).

#### FR-202: Per-run learning view
While viewing a run in `/console/runs/:id`, the system shall surface the
run-scoped learning data (`GET /api/v1/task-runs/{id}/learning`): gradients
produced, patterns, injected failure modes.

### Phase 3 — Dispatch & Control

#### FR-300: Dispatch composer
When the user opens `/console/dispatch`, the system shall present recipes and
per-lane evidence (win rate + CI + cost) from `GET /api/v1/dispatch/options`,
optionally scoped by `task_class`, so the user can compare lanes before
launching.

#### FR-301: Dispatch decision & launch
When the user confirms a dispatch, the system shall record the
conductor-vs-human decision (`POST /api/v1/dispatch`) and launch the run
(`POST /api/v1/runs`, Bearer-token auth), then route the user to the run's
conversation/detail view.

#### FR-302: Planner Q&A (HITL)
While a run is paused awaiting planner questions, when the profile
(`GET /api/v1/task-runs/{id}/profile`) exposes `human_questions`, the system
shall render them and submit answers via
`POST /api/v1/task-runs/{id}/answers`.

#### FR-303: Steering
While a run is in flight, when the user submits a steering message, the system
shall call `POST /api/v1/task-runs/{id}/steer` (Bearer-token auth) and confirm
injection.

#### FR-304: Cost pause / resume
While a run is active, when the user requests a cost halt, the system shall call
`POST /api/v1/task-runs/{id}/pause-cost`; when resumed, `…/resume-cost` — both
Bearer-token auth — and reflect the paused/resumed state in the UI.

#### FR-305: Stop / kill
While a run is active, when the user requests termination, the system shall call
`POST /api/v1/task-runs/{id}/stop` (SIGTERM) or `…/kill` (SIGKILL) with an
explicit confirm step before kill.

### Phase 4 — Trajectory & Distillation

#### FR-400: Trajectory analytics
When the user opens `/console/trajectory`, the system shall render cost-by-day
(`/trajectory/cost-by-day`), wall-time trends (`/trajectory/wall-time`), and
recent gradients (`/trajectory/gradients`).

#### FR-401: Self-improve history
While in the trajectory section, the system shall list self-improve runs
(`/trajectory/self-improve`) and render single-run detail with parsed notes,
children, siblings, and linked task_run (`/trajectory/self-improve/{run_id}`).

#### FR-402: TraceOtter distillation
Where TraceOtter data is available (`available: true`), the system shall render
the distillation funnel (`/traceotter/summary`), mined skills
(`/traceotter/skills`), and episodes with the training-set filter
(`/traceotter/episodes?imitate_only=true`).

#### FR-403: Idea tree & fingerprints
The system shall render the idea-tree explorer (`/idea-tree/roots`,
`/idea-tree/{root}`, node + ancestors) and recipe fingerprints
(`/fingerprint`, `/fingerprint/recipes`, `/fingerprint/lanes`).

#### FR-404: Projects switcher
The system shall list project homes (`/projects`), validate/browse paths
(`/projects/browse`, `/projects/validate`), and add/switch/remove a project
home (`/projects/add`, `/switch`, `/remove`) without a server restart.

---

## Non-Functional Requirements

### Performance
- Fleet/run-list views: first meaningful paint < 1s against a local runtime;
  list queries paginated (default page ≤ 50 rows).
- SSE stream (`/api/v1/stream`): consume the 2s-poll / 15s-keepalive cadence
  without UI jank; coalesce bursts and cap in-memory event buffer per run.
- DAG live-status updates applied incrementally (no full refetch per event).
- Native API client requests deduped/cached via TanStack Query (already in the
  FE stack); stale-while-revalidate for read panels.

### Security
- All **write/control** endpoints (`/runs`, `/dispatch`, `/steer`,
  `/pause-cost`, `/resume-cost`, and any launch) require the runtime's
  Bearer token; the FE shall attach it from the existing secrets/settings
  mechanism and never log it.
- Destructive actions (`/kill`, `/stop`, project `/remove`) require an explicit
  confirm interaction.
- PTY WebSocket only offered when `MO_PTY_ENABLED=1`; the FE must degrade
  gracefully (hide the terminal tab) when disabled.
- No mini-ork native data is exposed unless the Console feature flag is on.

### Scalability / Limits
- Handle a fleet with hundreds of historical task-runs via server-side
  pagination + filters; never fetch the full history unbounded.
- Artifact/evidence file reads streamed or size-capped to avoid loading large
  blobs into the SPA.

### Compatibility / Rollback
- Additive to the OpenHands SPA: with the flag off, behavior is byte-for-byte
  the upstream experience.
- Native client isolated from the OpenHands generated client so upstream
  re-syncs don't collide.

---

## Acceptance Criteria

### AC-001: Console is opt-in and invisible when off
Given `VITE_MINIORK_CONSOLE` is unset,
When the user loads the SPA,
Then no "Console" nav entry renders and no `/api/v1/*` request is issued.

### AC-002: Launch a run and watch it as a conversation
Given the Console is enabled and a recipe is selected in `/console/dispatch`,
When the user confirms dispatch,
Then a run is spawned via `POST /api/v1/app-conversations`, the user lands on
the conversation view, and streamed events appear without manual refresh.

### AC-003: Live DAG reflects node lifecycle
Given a run is executing,
When a node transitions running→done→failed,
Then `/console/runs/:id` updates that node's status within one SSE cadence
(≤ ~3s) without a full page reload.

### AC-004: Failure diagnosis is reachable
Given a run has failed,
When the user opens its detail,
Then the `why` diagnosis and `correlation` health are shown, explaining any
empty panels.

### AC-005: Learning loop is visible
Given historical learning data exists,
When the user opens `/console/learning`,
Then summary counts render and each sub-panel (bandit, GEPA, failures, circuit
breakers, …) loads its endpoint's data or an explicit "no data yet" empty state.

### AC-006: Control actions are authenticated and confirmed
Given a run is active and the Bearer token is configured,
When the user clicks Steer / Pause-cost / Stop,
Then the corresponding authenticated POST succeeds and the UI reflects the new
state; and When the user clicks Kill, Then a confirm dialog is required first.

### AC-007: Missing token is handled, not silently dropped
Given no Bearer token is configured,
When the user attempts a control action,
Then the UI blocks the action with a clear "authentication required" message
rather than firing an unauthenticated request.

### AC-008: PTY absence degrades gracefully
Given `MO_PTY_ENABLED` is not set,
When the user opens a run,
Then the terminal tab is hidden (or shows a disabled-state note), and no failing
WebSocket connection is attempted.

### AC-009: TraceOtter/optional panels handle "not run yet"
Given TraceOtter has never run (`available: false`),
When the user opens the distillation panel,
Then an informative empty state renders instead of an error.

---

## Error Handling

| Error Condition | HTTP / Signal | UI Behavior |
|-----------------|---------------|-------------|
| Native endpoint unreachable (runtime down) | network / 502 | Non-blocking banner "mini-ork runtime unreachable on :7090"; retry with backoff |
| Missing/invalid Bearer token on a control write | 401/403 | Block the action; prompt to set token in settings; do not retry blindly |
| Run/task not found | 404 | "Run not found — it may have been pruned"; return to fleet |
| SSE disconnect | stream close | Auto-reconnect with backoff; show "reconnecting…" chip; no data loss on the DB-backed tail |
| PTY disabled | feature off | Hide terminal tab (AC-008), no WS attempt |
| Empty correlation / no events | 200 empty | Show `correlation` diagnostic panel, not a spinner-forever |
| Optional data source absent (TraceOtter/learning empty) | 200 `available:false` | Explicit empty state (AC-009) |
| Kill/stop on an already-finished run | 409/no-op | "Run is not active"; refresh state |
| Large artifact/evidence read | size cap | Offer download link instead of inline render past N MB |
| Projection spawn failure | 5xx from `/app-conversations` | Surface backend error message; keep the user in `/console/dispatch` |

---

## Implementation TODO

### Phase 0 — Foundation
#### Backend
- [ ] Implement `POST /api/v1/app-conversations` in `mini_ork/web/routes/agent_server.py` (or a new `projection.py`): mint run_id, spawn detached, remote-START, return conversation descriptor.
- [ ] Ensure the projection maps run events → agent-server event shape consumed by the conversation view (reuse `stream.py` tail).
- [ ] Confirm Bearer-token dependency is shared/reusable for all control writes (`deps.py`/`auth.py`).
#### Frontend
- [ ] Add `ui/src/api/miniork/` client (typed, isolated from `@openhands/typescript-client`), base-URL aware (proxy in dev).
- [ ] Register Console routes in `ui/src/routes.ts` under `/console/*`.
- [ ] Add "Console" nav entry gated by `VITE_MINIORK_CONSOLE`.
- [ ] Wire TanStack Query keys + default caching/stale policy for native reads.

### Phase 1 — Run Observability
#### Frontend
- [ ] `FleetView` (`/console/fleet`): active runs + task-run list (filters) + summary header.
- [ ] `RunDetail` (`/console/runs/:id`): DAG viz with live node status; inspector for events/llm-calls/agents/agent-detail.
- [ ] Inputs/artifacts browser (fs + DB registry) with size-capped rendering.
- [ ] `WhyPanel` + `CorrelationPanel`.
- [ ] SSE subscription hook (`/api/v1/stream?task_run=`) feeding DAG + fleet.
- [ ] PTY terminal binding to existing xterm (guarded by `MO_PTY_ENABLED`).
- [ ] Recovery projection panel.

### Phase 2 — Learning Loop
#### Frontend
- [ ] `/console/learning` shell + summary header.
- [ ] 14 sub-panels (bandit, gepa, failures, patterns, topology, memory, gates, reviews, conductor, workflows, benchmarks, research, epic-dependencies, circuit-breakers) each with an empty-state.
- [ ] Per-run learning tab inside `RunDetail`.

### Phase 3 — Dispatch & Control
#### Frontend
- [ ] `DispatchComposer` (`/console/dispatch`): lane evidence table (win-rate/CI/cost), task_class scoping.
- [ ] Dispatch confirm → `POST /dispatch` + `POST /runs`, then route to run view.
- [ ] Planner Q&A panel (profile `human_questions` → `/answers`).
- [ ] Steer input; pause-cost/resume-cost toggle; stop/kill with confirm-on-kill.
- [ ] Bearer-token guard UX (AC-006, AC-007).

### Phase 4 — Trajectory & Distillation
#### Frontend
- [ ] `/console/trajectory`: cost-by-day, wall-time, gradients charts (recharts or the OpenHands chart lib).
- [ ] Self-improve history + detail.
- [ ] TraceOtter funnel/skills/episodes with `available:false` handling.
- [ ] Idea-tree explorer + fingerprint viewer.
- [ ] Projects switcher (browse/validate/add/switch/remove).

### Testing (per phase)
- [ ] Unit tests for the native API client (request shape, token attach, error mapping).
- [ ] Component tests for each panel's loading/empty/error states.
- [ ] Playwright E2E extending `tests/ui/` against a mock or live runtime: launch→observe→control happy path (mock-llm config already exists).
- [ ] Contract test that projection events render in the conversation view.
- [ ] Flag-off regression: SPA identical to upstream when Console disabled.

---

## Coverage-Panel Amendments (folded 2026-08-26)

Sixteen new FRs from `specs/openhands-native-surface.coverage.md`, grouped by
phase. Each cites `file:line` evidence and its lens consensus. These are
authoritative build requirements; the coverage doc is their audit trail.

### Phase 0 — Foundation + seam (folded)

- **FR-NEW-01 — Health-driven degradation (P0).** When the Console shell mounts,
  the FE shall poll `GET /api/v1/health` (`fleet.py:21`) and drive the
  runtime-unreachable banner + per-panel degraded states from it. Adjudicates
  D2 in favour of exposing health (every native panel needs a degraded-state
  source; cost of exposure is nil). (opus §3.1; 2/3)
- **FR-NEW-02 — Project read+switch in shell chrome (P0, pulled forward from
  P4).** When the user switches the active project (`GET /api/v1/projects`,
  `POST /api/v1/projects/switch`, `projects.py:72,159`), the FE shall invalidate
  ALL native query caches and re-establish SSE subscriptions — the switch
  changes which `state.db` every panel reads (`projects.py:159-164`).
  Read+switch move to P0/P1; browse/add/remove stay P4 (rest of FR-404).
  (opus §3.2 + §2; 3/3 on the capability)
- **FR-NEW-11 — Per-endpoint auth table (P0 spec change).** The security NFR's
  "all writes Bearer" is contradicted by the backend and the "additive only"
  rule forbids a server-side fix. Bearer: `/runs`, `/steer`, `/pause-cost`,
  `/resume-cost`. Loopback-trust: `/stop`, `/kill` (`control.py:9-11`).
  Unauthenticated: `/answers` (`control.py:65-74`), all project writes
  (`projects.py:152`). The FE shall gate its token UX per-endpoint from this
  table; AC-006/AC-007's uniform-token assumption is false (see NFR amendment
  below). (opus §3.11, glm §7 corroborates; supersedes the uniform Security NFR)
- **FR-NEW-13 — Tool-list contract (P0).** The shim reports `version:"1.39.1"`
  with `usable_tools:[]` (`agent_server.py:122,127`); the probe treats a
  non-array as "all tools available", so `[]` makes `isAgentServerToolAvailable`
  return false-for-all. Document the contract and consider returning `None`
  (→ "unknown") so the FE never silently enables tool affordances the backend
  can't honor. (minimax M2 + opus §1; 2/3)
- **FR-NEW-15 — Projection-seam re-scope (P0).** Folded inline into FR-002 above
  (full conversation-lifecycle projection, not one POST). (opus §2; 3/3)
- **FR-NEW-16 — Planner-QA seam decision (P0).** The spec shall decide in P0
  whether planner Q&A (polled via `/profile`, `control.py:54-63`) is projected
  into chat (requires synthesizing question events) or stays a native panel — it
  is the single flow that most feels like chat. **Recommendation: native
  PlannerQAPanel** (polling flow, typed inputs, post-answer next-step) with an
  optional chat deep-link; do not synthesize fake chat events in P0. (opus §4)

### Phase 1 — Observability (folded)

- **FR-NEW-03 — Run lifecycle state machine (P1).** When rendering run controls,
  the FE shall implement the status machine
  (`executing/verifying/reviewing/published/rolled_back/failed` + `stale`,
  `run_detail.py:22-48`; both staleness regimes `fleet.py:18` 6h /
  `run_detail.py:26` 30min) and render stop/kill/steer/pause only in valid
  states. (opus §3.3 + §2)
- **FR-NEW-04 — Evidence resolution (P1).** WhyPanel evidence paths shall resolve
  through `GET /task-runs/{id}/evidence?path=` (`run_detail.py:175-189`) with
  403/404 handling, else evidence links dead-end. (opus §3.4 / minimax)
- **FR-NEW-05 — Bridge-provenance honesty (P1).** When an event or LLM-call row's
  `bridge` is `time-window` (`run_detail.py:213-216,514,555`), the FE shall
  visually mark it approximate with a link to the correlation panel — otherwise
  the UI confidently attributes another run's LLM spend. (opus §3.5 + minimax)
- **FR-NEW-06 — Snapshot-then-subscribe (P1).** When opening any live view, the
  FE shall REST-fetch a snapshot then subscribe to SSE from `hello`
  (`stream.py:63`) and refetch on reconnect — the cursor starts at `MAX(id)` and
  emits only post-connect events (`stream.py:45-53`). The fleet view shall use
  the UNFILTERED stream (FR-105 currently names only `?task_run=`). (opus §3.6 +
  minimax; amends FR-105)
- **FR-NEW-07 — Live cost ribbon (P1).** When a run is active, run detail shall
  accumulate `mo_events.cost_usd` (`stream.py:76`) into a live spend ribbon and
  show the pause-threshold state when a cost-pause sentinel exists
  (`control.py:80-101`) — cost governance is a headline claim with a control-only
  surface today. (opus §3.7)
- **FR-NEW-12 — Family attribution in P1 (P1).** Lane/family attribution is
  already merged into run-DAG node rows (`run_detail.py:610-623`); it shall
  render on nodes in Phase 1. The P4 fingerprint viewer covers cross-recipe
  browsing only (fixes a P4 orphan). (opus §3.12)
- **FR-NEW-14 — PTY semantics amendment (P1).** FR-106 shall state that
  `/api/v1/pty` is a per-(project,harness) orchestrator attach over a persistent
  tmux session (`pty.py:107-138`), NOT a per-run terminal; the FE shall not
  advertise terminal-per-run and shall render the AC-008 disabled-state on the
  4403 `MO_PTY_ENABLED` close. (minimax M3 + opus §1 + glm; amends FR-106,
  resolves D4 → single project terminal tab)

### Phase 3 — Dispatch & control (folded)

- **FR-NEW-08 — Dispatch run_id threading (P3).** When launching after compose,
  the FE shall thread the `/dispatch`-minted `run_id` (`dispatch.py:247`) into
  the launch call (`runs.py:28` / app-conversations) so
  `conductor_decisions.task_run_id` joins, and display derived
  `decided_by`/`overrode` (`dispatch.py:253-257`) — else labelled-example
  capture silently breaks. (opus §3.8 + §2)
- **FR-NEW-09 — Evidence-honesty rendering (P3).** When a lane/topology has
  `evidence:"none"` (n<`MIN_SAMPLES=5`, `dispatch.py:136-140,183-187`), the
  composer shall render "insufficient evidence (n<5)" and never a bare rate or
  bar. (opus §3.9 + minimax)
- **FR-NEW-10 — Conductor calibration (P3).** The conductor panel shall surface
  decisions with `outcome='pending'` and their eventual `realized_score`
  (`dispatch.py:276`, `learning.py:379-389`) — the loop the dispatch endpoint
  exists to feed. (opus §3.10)

### Shared-primitive build notes (not FRs; blocking dependencies)

1. **DagView** — one graph renderer reused by FR-101 run DAG, FR-107 recovery
   overlay, FR-403 fingerprint viewer, FR-NEW-12 chips. Biggest single component
   gap; no graph component exists in `ui/src/`. Build first in P1.
2. **TextViewer** — markdown/yaml/json/log renderer with size cap, reused by
   `/inputs`, `/artifacts`, `/evidence`, `/why`. Build second in P1.
3. **Chart-lib decision** (recharts vs OpenHands-bundled) blocks FR-400 charts
   AND the FR-201 `/topology` quadrant scatter — no chart component exists in
   `ui/src/`. Decide in P2/P3 so it does not orphan the topology panel to P4.

### NFR amendment — auth posture is per-endpoint, not uniform

The "Security" NFR line ("All write/control endpoints … require the runtime's
Bearer token") is **factually wrong against the backend** and, under the
additive-only rule, cannot be fixed server-side. Replace the uniform assumption
with the FR-NEW-11 per-endpoint table. AC-006/AC-007 shall be read against that
table: Bearer-gated actions block-without-token; loopback/unauth actions
(`/stop`, `/kill`, `/answers`, project writes) do not require a token and the FE
must not present a false "authentication required" gate for them.

### Architecture note — runs are DAGs, not lines (D5)

The "runs-as-conversations" projection breaks for parallel fan-out: flattening
concurrent nodes into one chat timeline misrepresents causality and buries
per-node failure. Resolution: the DAG panel (`run_detail.py:592-654`) is
canonical for structure; the conversation view carries only the linearizable
surface (narrative events, terminal) with bidirectional deep links — **no
per-node chat threads**. Steer authority (D6): if both a chat input and the
native steering form can inject (`control.py:123-136`), the native steering form
is authoritative; the chat input, if present, routes through the same
`/steer` call (no second injection path). Bearer-token storage (D7) must NOT
resolve to the shim's process-local settings store (`agent_server.py:59-69`) or
tokens vanish on every restart — use the OpenHands secrets manager.

## Out of Scope
- Rewriting or replacing the OpenHands conversation UI (reused as-is).
- Changing existing `/api/v1/*` semantics (additive projection endpoints only).
- Auth/identity beyond the runtime's existing Bearer token (no multi-tenant/RBAC).
- Removing the OpenHands agent-server compat shim.
- The dead `ui.old/` tree (separate cleanup task).

## Open Questions (resolved by the coverage panel 2026-08-26)
- [x] Console as a **section inside** the SPA (one app, gated nav) vs separate
  route tree → **section inside**, gated by `VITE_MINIORK_CONSOLE` (FR-001/FR-003).
- [ ] Charting: reuse an OpenHands-bundled chart lib or add `recharts` (present
  in `ui.old/`)? → **STILL OPEN**, but now time-boxed: decide in P2/P3 (shared
  primitive #3) — it blocks FR-400 charts + the FR-201 `/topology` scatter.
- [x] Does `POST /api/v1/app-conversations` exist? → **NET-NEW in the shim**
  (confirmed: `agent_server.py:57` router has no such route). Backend build item
  in P0. Separately, glm's claim that `POST /api/v1/runs` is also missing is
  **wrong** — it exists at `runs.py:24-37` (D1 adjudicated against the tree).
- [x] Per-run learning (FR-202) in P1 or P2 → **P1**, co-located with run detail
  (`run_detail.py:236-343`, `injection_points[].wired` is load-bearing).
- [x] Bearer token source in the FE → **OpenHands secrets manager**, NOT the
  shim's process-local settings store (D7 — else tokens vanish on restart).
- [x] PTY per-run vs per-project (D4) → **single project terminal tab**;
  `/api/v1/pty` is a per-(project,harness) tmux attach, not per-run (FR-NEW-14).
