# Feature: Expose mini-ork's Native Surface in the OpenHands Agent-Canvas FE

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

#### FR-002: Run→conversation projection (spawn)
When a user creates a conversation whose target is a mini-ork run, the backend
shall accept `POST /api/v1/app-conversations`, mint a `run_id`, spawn the run
detached, remote-START it, and return a conversation descriptor consumable by
the existing OpenHands conversation view.

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

## Out of Scope
- Rewriting or replacing the OpenHands conversation UI (reused as-is).
- Changing existing `/api/v1/*` semantics (additive projection endpoints only).
- Auth/identity beyond the runtime's existing Bearer token (no multi-tenant/RBAC).
- Removing the OpenHands agent-server compat shim.
- The dead `ui.old/` tree (separate cleanup task).

## Open Questions
- [ ] Should the Console live as a **section inside** the conversation SPA (one app, one nav) or a **separate route tree** mounted at `/console`? (Assumed: same app, gated nav.)
- [ ] Charting: reuse an OpenHands-bundled chart lib or add `recharts` (present in `ui.old/`)?
- [ ] Does `POST /api/v1/app-conversations` already exist partially elsewhere, or is it net-new in the shim? (Discovery found the FE *expects* it; backend impl unconfirmed.)
- [ ] Do we want per-run learning (FR-202) in Phase 1 (co-located with run detail) or held to Phase 2?
- [ ] Bearer token source in the FE: reuse OpenHands secrets manager, or a dedicated mini-ork settings field?
