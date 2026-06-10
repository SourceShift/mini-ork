---
title: UI/UX Brief — mini-ork web dashboard (observability + control)
slug: web-dashboard-observability-control
route: http://127.0.0.1:7090/  (prod, served by FastAPI)
       http://127.0.0.1:7070/  (dev Vite — was :5173, recently aliased)
brief_type: adjustment (observability) + net-new (control)
audience: ui-ux-designer
created: 2026-06-09
author: claude (mini-ork repo session)
status: draft
related_files:
  - web/src/components/Shell.tsx
  - web/src/routes/FleetPage.tsx
  - web/src/routes/RunDetailPage.tsx
  - web/src/routes/TrajectoryPage.tsx
  - web/src/routes/SelfImproveDetailPage.tsx
  - web/src/routes/FingerprintPage.tsx
  - web/src/components/WhyCard.tsx
  - web/src/components/RunDag.tsx
  - web/src/lib/api.ts
  - web/tailwind.config.js
  - mini_ork/web/app.py
  - mini_ork/web/routes/fleet.py
  - mini_ork/web/routes/run_detail.py
  - mini_ork/web/routes/stream.py
  - mini_ork/web/agents.py          # NEW — per-run agent-tree assembler
  - bin/mini-ork-serve
  - docs/positioning/why-mini-ork.md
  - tests/test_web_smoke.py
---

# UI/UX Brief — mini-ork web dashboard

> **Brief type:** adjustment on observability (5 routes shipped, working) + **net-new on control** (no write surface exists today)
> **Read time:** ~12 min
> **Designer can start:** yes for observability adjustments; control track is **blocked on PM scoping** (section 14, Q1–Q3)
> **Critical pre-read:** Section 2 (Why now) + Section 14 (open questions). The "and control" half of the request changes the system's security and concurrency model in non-trivial ways.

---

## 1. TL;DR

**§17 supersedes §7** for global navigation. Designer should treat the sidebar as: `Overview · Runs (expandable list) · Trajectory · Fingerprint · Learnings`. The home page becomes a synthesis dashboard, not a registry.

mini-ork is a heterogeneous-family multi-agent framework with a SQLite substrate (`state.db`) and on-disk run artifacts (`.mini-ork/runs/<task_run_id>/`). A React 18 + TanStack SPA was shipped this week against a FastAPI surface — **strictly read-only** by design, GET-only, loopback-bound, no auth. Five routes cover fleet status, per-run forensics, self-improve trajectory, and the "detection fingerprint" (the framework's load-bearing positioning claim about model-family diversity).

**As of 2026-06-09 the backend also ships an agent-tree surface** (`GET /api/v1/task-runs/:id/agents` + `GET /api/v1/task-runs/:id/agents/:nodeId`) that the SPA does not yet consume. The user's stated requirement — *"navigate from each mini-ork run to the agents it dispatches, then each agent dispatch which other ones, what was their full LLM call logs, their status, if they failed, why failed, the exact duration of each"* — maps directly onto this new endpoint pair plus the recursive `run_spawns` table. **This is now the #1 design priority** (§16).

A **call inspector** (§18) — modeled on Claude Code's tool-call display — is required wherever calls are surfaced: per-agent drawer + per-trajectory iteration view. The `llm_calls` schema does not yet store request/response/tool-call content; engineering will land that extension soon. Designer mocks against the assumed shape today; visual language survives schema-detail changes.

The user asked for "full observability and control". The observability half is an **adjustment** pass — the UI exists and is functionally correct, but lacks the visual hierarchy, empty/error treatments, and density-tuning a designer would naturally bring. The control half is **net-new** and crosses a hard architectural boundary: SQLite writers vs. UI readers, single-orchestrator assumption, lack of auth on a loopback service. Before the designer can compose the control surface, PM has to settle three scope questions (section 14).

Success = an operator opens the dashboard during an active `recursive-self-improve` cycle and (a) understands fleet health in under 10 s, (b) can drill into any failure with one click and read evidence inline, (c) can act on the run (cancel / retry / promote / lock budget) **without leaving the dashboard** — once the control track is scoped.

## 2. Why now

Three forces converged this week:

1. **Self-improve runs went live.** `self_improve_runs` table now has 33+ iterations recorded (see recent commits `45580a7`, `77f965f`, `98deaa1`, `8f11814`). The CLI (`mini-ork metrics`) was the only inspection tool. Operators were running ad-hoc SQL against `state.db` to figure out which iteration converged, what its cost cap was, and why iter N+1 didn't spawn.
2. **The detection-fingerprint claim became load-bearing.** `docs/positioning/why-mini-ork.md` positions mini-ork as "heterogeneous-family by construction". The Fingerprint page is the proof-of-claim UI — it must communicate the coalition risk verdict (`heterogeneous` / `low` / `medium` / `high`) the way a credit score communicates risk: at a glance, with explainer copy.
3. **Cost ran up.** `total_cost_usd` is in the fleet header. Self-improve loops have a documented cost-cap pre-check (commit `77f965f`). Operators want a "kill the loop now" button, not a CLI invocation in another tab — which is the genesis of the "and control" half of the request.

## 3. Jobs to be Done

**Primary JTBD (observability):**
> When I'm running a self-improve or refactor-audit cycle, I want to see fleet health, per-run failure evidence, and the family-diversity fingerprint at a glance, so I can decide whether to let the cycle continue, intervene, or revert.

**Secondary JTBDs:**
- **Forensics:** When a run finishes with `verdict=REQUEST_CHANGES` or `status=failed`, I want every piece of failure evidence (execute.log, verifier-result-*.json, evidence logs, self_improve notes) aggregated on the run-detail Overview tab, so I don't have to `ls .mini-ork/runs/<id>/` and grep through six files.
- **Trajectory:** When I'm pitching the framework to a peer, I want to point at the trajectory page and show "look — cost down, wall-time stable, convergence happening" across the last 7 days, so the "mini-ork is just more agents" objection dies.
- **Fingerprint receipts:** When a reviewer asks "is your recursive-self-improve recipe heterogeneous?", I want a single URL to share that displays the families per node and the coalition risk badge.
- **Control (net-new — pending scope):** When a loop is heading past its cost cap or producing repeated REQUEST_CHANGES verdicts, I want to abort / pause / promote-now from the UI instead of `kill -TERM` + manual git surgery.

## 4. Users

| User type | Proficiency | Frequency | Context | Permission today |
|---|---|---|---|---|
| **Framework operator (you)** | Expert in mini-ork internals, schemas, recipes | Many times per day during dev cycles | Desktop, focused, dark mode, single monitor | loopback-only, no auth |
| **Research collaborator / reviewer** | Knows multi-agent ideas, doesn't know schemas | Episodic — when sharing fingerprint receipts or trajectory plots | Desktop, distracted, screenshot consumer | None today (loopback) |
| **Future ops persona** (post-control track) | Operations-leaning; reads dashboards, writes some scripts | Continuous during long-running cycles | Desktop, possibly multi-monitor with terminal beside | TBD — see § 14 Q2 |

Note: there is **no admin gate, no login, and no session model** in the FastAPI app (`mini_ork/web/app.py`). The loopback binding *is* the authorization model. Any UI control affordance has to declare an answer for "who is allowed to click this and how do we know" — section 14 Q1.

## 5. Current state inventory

### 5a. Files involved

| Role | File | What it contributes |
|---|---|---|
| **Shell + nav** | `web/src/components/Shell.tsx` | sidebar nav (Fleet / Trajectory / Fingerprint), brand mark, live `state.db` health pill |
| Page | `web/src/routes/FleetPage.tsx` | 4 stat cards, active-runs table, recent task_runs table |
| Page | `web/src/routes/RunDetailPage.tsx` | 5 tabs (Overview / DAG / Artifacts / Events / LLM calls), correlation diagnostics, SSE-driven live invalidation |
| Page | `web/src/routes/TrajectoryPage.tsx` | 2 Recharts area charts (cost/day, wall-time/day) + self-improve ledger table |
| Page | `web/src/routes/SelfImproveDetailPage.tsx` | per-iteration: timeline, deadlines, worktree, parsed notes, linked task_run, sibling nav |
| Page | `web/src/routes/FingerprintPage.tsx` | recipe selector → coalition verdict card + family histogram + per-node attribution table |
| Component | `web/src/components/WhyCard.tsx` | failure-evidence aggregator: verifiers, evidence refs, self-improve notes, execute.log failure lines + tail |
| Component | `web/src/components/RunDag.tsx` | `@xyflow/react` DAG with longest-path layering + node status colors (running/done/failed/pending) |
| Component | `web/src/components/ArtifactViewer.tsx` | browse + read text artifacts from `.mini-ork/runs/<id>/` |
| Component | `web/src/components/Pill.tsx` | `StatusPill` / `VerdictPill` / `FamilyPill` shared tokens |
| Hook | `web/src/lib/sse.ts` | EventSource wrapper for `/api/v1/stream?task_run=...` |
| Data | `web/src/lib/api.ts` | typed client over the 20+ GET endpoints |
| Format | `web/src/lib/format.ts` | `formatCost`, `formatDuration`, `formatRelative`, `familyColor` |
| Tokens | `web/tailwind.config.js` + `web/src/index.css` | `ink-{50..900}` neutrals, `ork.{red,green,amber,slate}` brand, dark-only |
| Backend | `mini_ork/web/app.py` | FastAPI factory; CORS for `:7070` (new) and `:5173` (legacy); GET-only `allow_methods` |
| Backend routes | `mini_ork/web/routes/{fleet,run_detail,trajectory,fingerprint,stream}.py` | the read API; SSE polls SQLite at 1 s |
| Backend bridge | `mini_ork/web/why.py` | the failure-evidence aggregator; reads `execute.log`, `verifier-result-*.json`, and joins `execution_traces` |
| **Backend agent-tree** | `mini_ork/web/agents.py` (new) | per-run agent assembler: joins recipe DAG + `run_events` (node lifecycle) + `llm_calls` (grouped by `metadata_json.node_id`, fallback to `feature_name` suffix) + filesystem artifacts + `run_spawns` (recursive children). Exposes `list_agents` + `agent_detail`. |
| CLI | `bin/mini-ork-serve` | boots uvicorn; preflights `state.db`; default `127.0.0.1:7090` |
| Tests | `tests/test_web_smoke.py` | smoke-tests the route handlers directly against the repo's own `state.db`; **a designer can run `pytest tests/test_web_smoke.py` to validate the data layer survives their changes** |

### 5b. Existing design assets

| Asset | Status |
|---|---|
| Static HTML mockups | **None.** This repo has no `design/v1/` dir, unlike the user's other projects. |
| JSX prototypes | **None.** |
| Screenshots in repo | **None** in `docs/`. |
| Brand reference | `docs/positioning/why-mini-ork.md` is the prose brand voice. The visual brand is encoded in `tailwind.config.js`: the "mini-ork" hero image (red master orc + green mini-orcs) gave the `ork.{red,green,amber,slate}` palette. **No logo file exists.** The "mO" letterform in `Shell.tsx:18-21` is the current brand mark. |
| Live UI | `pnpm --dir web dev` then open `http://localhost:7070`. Smoke-able against the repo's own `.mini-ork/state.db` which has 33+ self-improve iterations recorded. |

**Treat the visual track as net-new.** The current SPA is engineer-built — it works, it's tokenized, it's accessible-by-default, but it has not been through a designer's hands. There's no Figma file, no design tokens doc, no spacing scale beyond Tailwind defaults, no motion guidelines, no logo system.

### 5c. Related docs

- `docs/positioning/why-mini-ork.md` — the **load-bearing positioning doc**. Quote a designer can borrow verbatim for the Fingerprint hero: *"If the list reads 'Sonnet, Sonnet, Sonnet, Sonnet, Opus' you have an evaluative coalition, not an audit."*
- `web/README.md` — operator-facing setup + route map.
- `Makefile` — `make test-web` runs the smoke tests.
- Recent commits `45580a7..77f965f` — context on what's recently shipped on the self-improve side (drives trajectory page expectations).

## 6. Heuristic audit (current observability UI)

| # | Heuristic | Issue | Evidence |
|---|---|---|---|
| 1 | **H1 Visibility of system status** | The Fleet page has *no* skeleton state on first load — all four stat cards display `0` until `summary.data` resolves, then snap to real numbers. Operator can't tell "is the DB empty" from "is it loading". | `web/src/routes/FleetPage.tsx:26-28`, `web/src/routes/FleetPage.tsx:55-60` — `value={totalRuns}` is unconditional. |
| 2 | **H1 Visibility (live updates)** | RunDetailPage uses SSE to invalidate queries but there's no visible "live" indicator. An operator watching a long run can't tell if they're on stale data or the stream is connected. | `web/src/routes/RunDetailPage.tsx:30-39` — `useEventStream` fires but no UI hook. |
| 3 | **H2 Match between system and the real world** | Column labels are schema-leaking: `verdict`, `recipe`, `task_class`, `workflow_version`, `kickoff_path`. Internal users tolerate this; the trajectory page is the public-facing receipts artifact and would benefit from prose explanations. | `web/src/routes/FleetPage.tsx:106-117` headers; `web/src/routes/TrajectoryPage.tsx:106-115`. |
| 4 | **H5 Error prevention / H9 Recovery** | When `correlation.bridge_methods` does not include `mo_events.trace_id` (i.e. no trace ID was written), the run-detail Events tab shows a time-window-best-effort table that **may contain events from concurrent runs**. The CorrelationCard explains this, but the events table itself doesn't visually flag rows as "may not belong to this run." | `web/src/routes/RunDetailPage.tsx:150-180` (Events tab) + `web/src/lib/api.ts:88-94` (BridgeMethod) + `mini_ork/web/routes/run_detail.py:79-128` (the 3 bridge methods). Each row carries `bridge: "trace_id" \| "run_id" \| "time-window"` — the badge exists but is small (10 px) at end-of-row. |
| 5 | **H6 Recognition rather than recall** | The Fingerprint page front-loads recipe names as bare pills. There's no recipe description, no "last run was N hours ago" affordance, no quick-link to the most recent `task_run` for that recipe. Operator has to remember which recipe is which. | `web/src/routes/FingerprintPage.tsx:63-78`. |
| 6 | **H8 Aesthetic and minimalist** | The RunDetailPage Overview tab packs 12 cells (recipe / class / status / verdict / cost / duration / created / ended / trace / kickoff / plan / artifact) into a 6-column grid. Half are file paths truncated to 200 px. **The information is correct; the hierarchy is flat.** | `web/src/routes/RunDetailPage.tsx:62-86`. |
| 7 | **H10 Help and documentation** | Coalition verdicts are explained inline (good) but the page doesn't link to `why-mini-ork.md` or the supporting arxiv papers. A reviewer landing on the URL cold has no entry point. | `web/src/routes/FingerprintPage.tsx:5-28`. |

I deliberately did not call out the other heuristics — they're either fine (consistency, freedom from constraints) or non-applicable (admin recovery flows don't exist yet).

## 7. Information architecture

### Current — global

```
Shell (sidebar)
├── mO brand mark (Shell.tsx:14-25)
├── nav
│   ├── Fleet (Activity icon)            → /
│   ├── Trajectory (Telescope icon)      → /trajectory
│   └── Fingerprint (Network icon)       → /fingerprint
└── footer
    ├── db path (truncated, monospaced)
    └── db connection pill (green/red)
```

### Current — per page

**`/` Fleet**

- H1: "Fleet"
- 4 stat cards: `task_runs` / `executing` / `failed` / `total spend`
- Section "Active runs" (heartbeat-tracked, polls 3 s) — empty state when zero
- Section "Recent task_runs" (polls 5 s, limit 100) — clickable rows → `/runs/:id`

**`/runs/:taskRunId`**

- Breadcrumb: `fleet / <task_run_id>`
- 6-col metadata grid (12 cells)
- Tab strip: Overview / DAG / Artifacts / Events / LLM calls
- Overview tab: WhyCard + RunSummary + CorrelationCard (in that order)
- DAG tab: coalition + family banner + ReactFlow canvas
- Artifacts tab: file tree + viewer
- Events / LLM calls: scrollable tables with bridge-method badges

**`/trajectory`**

- H1: "Trajectory"
- 2-up Recharts area charts (cost/day, wall-time/day)
- self_improve_runs ledger — rows link to detail page

**`/trajectory/self-improve/:runId`**

- Breadcrumb: `trajectory / self-improve / iter N`
- Header: outcome pill + deadline pills + sibling nav (prev/next iter)
- 2-up cards: Timeline / Worktree
- Linked task_run card (4-col grid, opens `/runs/:id`)
- Notes section: parsed kv/flag/sha items + raw notes details
- Descendant iterations list

**`/fingerprint`**

- H1 + epigraph (the load-bearing quote)
- Recipe selector pills
- Coalition verdict hero card (icon + label + explainer + stats)
- Family distribution row
- Per-node attribution table

### IA smells

- **Cross-page navigation gaps.** `/runs/:id` has no link back to the recipe's fingerprint, even when `dag.data.recipe` is right there. Round-trip would let an operator validate "did this specific failure happen on a heterogeneous-family DAG or not?" in one click.
- **Self-improve isn't in the sidebar.** It's reachable only via Trajectory → table row. A frequent operator workflow ("watch the current self-improve loop") has no top-level entry.
- **No global search / command palette.** Tab between three task_runs by ID requires going back to `/` each time. With 100s of runs in `state.db` this gets old fast.
- **The `/api` discovery endpoint is invisible to the UI.** `mini_ork/web/app.py:60-75` exposes a self-documenting endpoint index — nothing in the SPA surfaces it. Designer might want a developer-mode panel that shows "this view is fed by these endpoints" for debugging.

## 8. State machine — per data source

The single biggest gap in the current UI is **empty-state and error-state design.** Loading states are partially handled by TanStack Query but the visual treatments are inconsistent.

### Data source: `api.taskRunsSummary` (Fleet stat cards)

| State | Current behavior | Required behavior |
|---|---|---|
| Cold | Cards show `0` (because `?? 0` defaults) — indistinguishable from "no runs yet" | Skeleton shimmer over the number |
| Loading (refetch) | Silent — numbers may jitter | No visible change unless number changes; if changed, brief highlight pulse |
| Empty (`task_runs` table empty) | Cards show `0`, tables show "No active runs"/"No task_runs yet." | A single full-page empty state: "Run `mini-ork classify <kickoff>` to see your first task_run here." with link to README |
| Error (5xx) | TanStack returns `error`, but **no UI branch reads it on FleetPage** | Stat cards show `—` with a small ⚠ icon; tooltip shows error; sidebar pill flips red |
| Success | Live numbers | unchanged |

### Data source: `api.activeRuns` (Fleet "Active runs" section)

| State | Current | Required |
|---|---|---|
| Cold | empty paragraph | Skeleton row × 2 |
| Empty | "No active runs (heartbeat-tracked)." | unchanged, but add hint: "Active runs are tracked via heartbeat to `state.db`. If you have a running `mini-ork` process but see nothing here, check `runs` table." |
| Stale heartbeat | Row stays in table indefinitely | Row dims after `last_heartbeat_at` > 60 s ago; "stale" pill replaces "active" |
| Live update | Polls every 3 s | unchanged but add small "live · 3 s" indicator next to section title |

### Data source: `api.taskRun(id)` + `api.events(id)` + `api.llmCalls(id)` (Run detail)

| State | Current | Required |
|---|---|---|
| 404 | Red "task_run not found" + back link | unchanged |
| Loading | TanStack default (no UI) | Tab content shows skeleton; metadata grid shows skeleton cells |
| Events present, **strict bridge** | Table populates, each row has small `trace` badge | unchanged but the strict badge should be more prominent on the Overview correlation card |
| Events present, **time-window fallback** | Table populates, rows have `window` badge | **Add a row-level visual flag** (e.g. row left border in `ork-amber`) so a quick scroll reveals "these might not be mine"; today the badge is at end-of-row and easy to miss |
| Events empty, trace_id absent | Empty table + "No events yet" paragraph + the CorrelationCard explains remediation | unchanged; this is well-handled |
| LLM calls empty | "No llm_calls correlated. Verify lib/llm-dispatch.sh writes..." | unchanged — strongest empty-state copy in the app, designer to preserve voice |

### Data source: `api.dag(id)` (DAG tab)

| State | Current | Required |
|---|---|---|
| Cold | empty box | Skeleton DAG (placeholder rectangles in expected layout) |
| Empty recipe | "No DAG for this recipe." | unchanged |
| All `never_seen` | Faded nodes (`opacity: 0.55`) | unchanged but add "expected start time" tooltip when next |
| Mid-run | Running node has pulsing dot + 2 px ring | unchanged — this is well done |
| Failed | Red ring | Add subtle background tint to make failed branch stand out at scale |

### Data source: `api.selfImproveDetail(id)` (Self-improve detail)

| State | Current | Required |
|---|---|---|
| Loading | "loading…" text | Full skeleton matching the layout |
| `in_progress` past **soft** deadline | Amber pill: "soft deadline passed" | unchanged but add countdown to hard deadline |
| `in_progress` past **hard** deadline | Red pill: "hard deadline passed" | unchanged but **add an interrupt CTA** (control track — pending scope) |
| Has `parent_run_id` | Mono link "iter N-1 parent" | unchanged; consider visual lineage breadcrumb across siblings |
| Linked task_run absent | Section hidden | unchanged |

### Data source: `api.fingerprint(recipe)` (Fingerprint page)

| State | Current | Required |
|---|---|---|
| Loading | Recipe list still selectable, body blank | Skeleton verdict card |
| `heterogeneous` / `low` | Green border, ShieldCheck | unchanged |
| `medium` | Amber, AlertTriangle | unchanged |
| `high` (coalition) | Red, AlertTriangle | unchanged; consider adding a "what to do" section: which lanes to reassign |

### Data source: SSE `/api/v1/stream` (live updates)

| State | Current | Required |
|---|---|---|
| Connected | Invisible — invalidates queries silently | "Live" indicator: green pulse near page title |
| Disconnected | Browser auto-reconnects, invisible | Yellow pulse + "reconnecting…" if down > 5 s |
| Keepalive | Comment frame every 15 s, invisible | unchanged |

## 9. Accessibility baseline

| Check | Current | Required |
|---|---|---|
| **Color scheme** | `darkMode: "class"` + `<html class="dark">` — **dark-only.** No light mode. | Decide if light mode is in scope. If yes, the brand palette needs a parallel scale. If no, document the decision. |
| **Contrast** | Body text on `bg-ink-900`: `ink-100` (`#e9ecef` on `#0d0f10`) = WCAG AAA. Muted text `ink-400` (`#6c757d` on `#0d0f10`) = AA large only — **fails AA for body**. Used in many places (sidebar status, table sub-info). | Promote body-tier muted text to `ink-300` (`#adb5bd`) or restrict `ink-400` to non-content chrome (icons, separators). |
| **Focus rings** | Tailwind defaults — no `focus-visible:ring-*` token on buttons / tabs / nav links | Define a tokenized focus ring: `focus-visible:ring-2 focus-visible:ring-ork-amber focus-visible:ring-offset-2 focus-visible:ring-offset-ink-900`. |
| **Keyboard nav** | Sidebar links and buttons are native, so tab order works | Tab navigation across the tab strip (`RunDetailPage`) and table rows is fine; arrow-key nav inside `RunDag` is not wired (xyflow has it built-in but disabled because `nodesDraggable={false}`). |
| **Screen reader** | No `aria-live` on the live tables, no `aria-current` on the active sidebar nav link | Add `aria-current="page"` to active nav `Link`; `aria-live="polite"` region for the SSE-driven updates. |
| **`data-testid` coverage** | `FleetPage`, `Shell`, `WhyCard` have testids (recently added). `RunDetailPage`, `TrajectoryPage`, `FingerprintPage`, `SelfImproveDetailPage` do not. | Designer to propose the scheme; current scheme is `<area>-<element>` (e.g. `fleet-page`, `stat-task-runs`, `active-run-${id}`, `why-card`). Maintain it. |
| **Motion** | DAG running-node `animate-pulse` only. No reduced-motion handling. | Respect `prefers-reduced-motion: reduce` for any new motion. |

## 10. Constraints

### Technical (non-negotiable)

- **Stack:** Vite 5 + React 18 + TypeScript 5 + TanStack Router 1.46 + TanStack Query 5 + Tailwind 3 + `@xyflow/react` 12 + Recharts 2 + `lucide-react` + `react-markdown` + `remark-gfm`. See `web/package.json`. No shadcn, no MUI.
- **No CSS-in-JS, no styled-components.** Tailwind + the four `@layer components` utilities in `index.css` (`card`, `pill`, `pill-ok/warn/err/muted`).
- **Backend is GET-only.** `mini_ork/web/app.py:45` declares `allow_methods=["GET"]`. The route files contain only `@router.get`. Any control surface (POST/PATCH/DELETE) is **new work** in both layers — see § 14.
- **Database is SQLite WAL.** The dashboard opens it `PRAGMA query_only` (single-writer model). A control surface must coordinate with the orchestrator that holds the write lock — `bin/mini-ork-self-improve`, `bin/mini-ork-execute`, etc. Section 14 Q3.
- **Loopback by default.** `bin/mini-ork-serve` binds `127.0.0.1:7090`. `--host 0.0.0.0` is an opt-in. The "control" surface needs a story for "what happens when this is exposed".
- **No auth, no sessions, no CSRF token.** Loopback is the AuthN model today. Section 14 Q1.
- **Two dev-port histories:** CORS allows both `5173` (legacy Vite default) and `7070` (the project's current alias). Designer can use either, but mock setup docs should call out `7070`.
- **Single React entry.** `web/index.html` mounts `main.tsx`. No code-splitting yet. SPA bundle is served from `mini_ork/web/static/` after `pnpm build`.
- **snake_case from the API.** TS types in `web/src/lib/api.ts` mirror the SQL column names (`task_class`, `cost_usd`, `last_heartbeat_at`). Do not rename fields at the client; keep the wire shape literal.
- **No new i18n.** Single-language English UI. Literal JSX strings.

### Visual / brand

- **Dark-only today.** `darkMode: "class"` + `<html lang="en" class="dark">`. Light mode is not on roadmap.
- **Palette:**
  - Neutrals: `ink-{50..900}` — slate gradient, near-black `#0d0f10` background.
  - Brand: `ork.red #8b1e1e`, `ork.green #1e6b3a`, `ork.amber #b07a2c`, `ork.slate #3a4250`.
  - Status semantics: green = ok / heterogeneous, amber = warning / medium-risk, red = failed / coalition.
- **Type:** Inter sans (body), `ui-monospace` (code, IDs, schema labels).
- **Brand mark:** the "mO" letterform in `Shell.tsx:18-21` on an `ork.red/80` rounded square. No logo file; designer can propose one if they want.
- **The Fingerprint page IS the brand statement.** Designer should treat it as the marketing hero of the framework. The epigraph quote from `why-mini-ork.md` is the load-bearing copy.

### Performance budget

- First contentful paint < 1 s on localhost (no network).
- TTI < 1.5 s p90.
- No layout shift when SSE pushes events into tables.
- SSE polls SQLite every 1 s — cheap (WAL reads don't block writers). Keep it.

### Out-of-bounds for design

- No mobile responsive layout. Desktop-only operator tool.
- No theme toggle.
- No SaaS dashboard chrome (notifications, user menu, org switcher). Single operator, single repo, single `state.db`.
- No marketing variant.

## 11. Success metrics

| Flavor | Metric | Status |
|---|---|---|
| **User outcome** | Operator goes from "open dashboard" → "diagnose root cause of a failed run" in **≤ 3 clicks**, no terminal switch | proposed — confirm with PM |
| **User outcome (agents)** | Operator drills `run → failing agent → failing LLM call's error_message` in **≤ 3 clicks** (Agents tab → row → LLM calls tab in drawer). See §16i acceptance test. | proposed |
| **User outcome (call inspector)** | Operator reading a failed trajectory iter can identify the breaking tool call's name + args + output without scrolling past unrelated noise (standard density auto-expands failures — §18k.1) | proposed |
| **Render fidelity** | `Edit` tool calls render as unified diff with green/red gutters; operator can spot-check the change without opening the file (§18k.3) | proposed |
| **User outcome (lineage)** | When a run spawns sub-runs, the operator can navigate to any descendant within **≤ 2 clicks** from the parent | proposed |
| **User outcome** | Reviewer landing on `/fingerprint` cold understands "is this audit heterogeneous?" in **≤ 10 s** | proposed — confirm with PM |
| **Behavioral** | Operator opens the dashboard at least once per active recursive-self-improve cycle (currently: never, because the UI just shipped) | proposed |
| **Behavioral** | Number of `sqlite3 state.db "SELECT ..."` shell invocations during a debug session drops to 0 | proposed |
| **Technical** | TTI ≤ 1.5 s p90 on `pnpm dev` | proposed |
| **Technical** | Zero layout shift when SSE pushes a new event into the events table | proposed |
| **Brand** | The Fingerprint page is screenshotted into a blog post or social media share **without** edits being needed | proposed |

## 12. References (patterns worth borrowing)

- **GitHub Actions run summary** — the status-strip + per-job expandable card pattern. Matches the run-detail Overview tab's WhyCard intent.
- **Datadog APM trace explorer** — the bridge-method badges + warning row treatment for time-window fallbacks. Datadog flags "approximated" spans in exactly the same situation mini-ork has with missing `trace_id`.
- **Linear Cmd+K** — the missing global navigation primitive. Operators jump between task_runs all day.
- **Grafana panel-edit "table → state"** — for the empty/error/partial state copy voice. Grafana writes "No data" + a tiny "What's happening?" link in muted text. Sets the bar for what good empty states feel like.
- **The "credit score" UX pattern** — for the Fingerprint coalition verdict. A single number / label with explainer copy, supporting detail collapsed underneath. Equifax / Klarna do this well in radically different brand voices; mini-ork's version should be deeply on-brand (slate + red + austere typography).

## 13. Out of scope

- **Mobile layout.** Desktop-only operator tool.
- **Light mode.** Confirmed dark-only.
- **Multi-tenant / multi-`state.db` views.** Single repo per dashboard.
- **Marketing / landing variant.** Public-facing positioning lives in `docs/positioning/why-mini-ork.md`, not the UI.
- **Comment / annotation system** (e.g. "leave a note on this iter"). Substrate has no `notes` table for the UI.
- **Permission management UI.** No user model.
- **The CLI itself.** `bin/mini-ork-*` shell scripts are deliberately the source of truth for actions; the UI is the inspector.

## 14. Open questions for PM (BLOCKING the control track)

1. **Who is allowed to "control" mini-ork from the UI?** The FastAPI app has no auth. Three viable models:
   - (a) **Loopback-only with no auth** (cheapest, today's posture). Anyone on the operator's laptop can click the button. Acceptable for solo-operator use; unacceptable if `--host 0.0.0.0` is ever a doc'd path.
   - (b) **Bearer token in `.mini-ork/config/serve-token`**. Operator generates once; UI persists in `localStorage`. Defensible against accidental network exposure.
   - (c) **Full OS-user check via Unix-socket binding.** Heaviest; matches Docker daemon model. Probably overkill.
2. **What does "control" mean concretely?** Best-guess scope ladder, smallest to largest:
   - L1: **Soft control** — open the kickoff `.md` in the operator's `$EDITOR` via a `code://` or similar URI handler. Zero backend writes.
   - L2: **Process control** — abort an active `task_run` (sends SIGTERM to the orchestrator PID recorded in `runs.pid`). Requires single new endpoint, no SQLite write.
   - L3: **State control** — adjust cost cap on the running self-improve loop; pause; resume; promote-now. Requires writing to `state.db`, coordinating with the orchestrator's write lock.
   - L4: **Dispatch** — kick off a new `mini-ork classify <kickoff>` from the UI. Full submit-form UX, file picker, recipe selector. Big surface.
   - PM should pick L1–L4 explicitly. The designer's IA changes drastically between L1 and L4.
3. **Concurrency with the orchestrator's SQLite writer.** Today, SQLite WAL allows the UI to read while the orchestrator writes. The moment the UI also writes, we have **two writers contending for the same lock**, and the orchestrator was not designed to expect external writes. Possible answers: write a "control" event into a separate `ui_intents` table that the orchestrator polls; require the orchestrator to expose a Unix-socket command channel; defer control to filesystem signals (touch a `.mini-ork/runs/<id>/CANCEL` sentinel). **No "just call `UPDATE`" answer is safe.**
4. **Is light mode a constraint or an option?** Designer can build dark-only forever or set up the parallel scale now. Default-keep dark-only unless PM says otherwise.
5. **Does the Fingerprint page need to be public-shareable?** If yes, it eventually needs deep-link permalinks, OG-image generation, and accessibility-compliant fallback for the DAG (a table). If no, the current SPA-only treatment is fine.

## 15. Definition of done (design phase)

- [ ] **Call inspector** (§18) — Claude Code-style conversation + tool-call viewer; per-agent drawer surface + per-trajectory tab on `SelfImproveDetailPage`; compact/standard/verbose density modes; tool-aware renderers (`Bash`, `Read`, `Edit` diff, `Grep`, etc.); filter+search header. Mock against the assumed `/transcript` shape (§18a); coordinate with engineering on schema landing.
- [ ] **Sidebar redesign + expandable Runs section** (§17a, §17b) — small-N + large-N states, search field, active-row indicator, family dot, "View all" affordance
- [ ] **Overview synthesis dashboard** (§17c) at `/` — header strip, heterogeneity hero, active-runs strip, trajectory snapshot, recent learnings; including empty-state and `state.db unreachable` states (§17e)
- [ ] **Learnings explorer** (§17d) — `/learnings` index + `/learnings/:slug` markdown viewer (designer can mock against the existing docs/ files; backend endpoint is a parallel engineering task)
- [ ] **Agents tab + drawer** (§16) — mocks for all states in §16e, including the LLM-call failure-expansion treatment and the family-stripe affordance (§16g)
- [ ] **Lineage tab** (§16f) — indented-tree mock with depth-2 collapse pattern and "why?" deep-link
- [ ] Hi-fi mocks for all five existing routes covering every state in § 8 (in the new sidebar context)
- [ ] Empty-state and error-state copy proposed for every list/table
- [ ] Token system documented:
  - [ ] spacing scale (currently implicit Tailwind defaults)
  - [ ] type scale (3–4 sizes with intended use)
  - [ ] color tokens with semantic aliases (`--surface`, `--text-muted`, `--status-fail`, etc.) on top of the existing `ink-*` / `ork-*` palette
  - [ ] motion tokens incl. `prefers-reduced-motion` story
- [ ] Focus-ring + keyboard-nav annotations on every interactive
- [ ] Live-update indicator + reconnecting state proposed for SSE-driven views
- [ ] Cross-page nav additions:
  - [ ] Self-improve top-level sidebar entry
  - [ ] Per-run "view fingerprint for this recipe" deep link
  - [ ] Optional Cmd+K command palette
- [ ] Fingerprint page treated as the brand hero: typography + composition that reads as a receipt, not a settings page
- [ ] `data-testid` scheme extended to all routes following `<area>-<element>`
- [ ] **Decision document** for the control track (per § 14 Q1–Q3) — or explicit deferral: "control track punted to v2, design dark-only observability polish for v1"
- [ ] One-page **operator print-out / cheatsheet** (markdown) that mirrors the IA so a new operator can grok the dashboard offline

---

## 16. Agent transparency & lineage (NEW — #1 priority)

> **User request, verbatim (2026-06-09):**
> *"We need full transparency on the agent runs so from the UI/UX I can navigate from each mini-ork run to the agents it dispatches, then each agent dispatch which other ones, what was their full LLM call logs, their status, if they failed, why failed, the exact duration of each, etc."*

This section trumps everything else in the brief if scope has to be cut. The backend data is ready; the UI needs to consume it.

### 16a. JTBD

> **Primary:** When a task_run reports `verdict=REQUEST_CHANGES` or `status=failed`, I want to drill from the run → the exact agent (recipe node) that broke → the LLM call that produced the failure → the error message or finish reason, in **no more than 3 clicks**, so I can rule out "model A was sandbagging" vs "the prompt was wrong" vs "the recipe wiring was wrong."

**Secondary jobs:**

- **Lineage trace:** *"Show me every sub-run this iter spawned, and which agent inside this run was responsible for spawning each."* (Recursive `run_spawns` joined by `parent_run_id` + `node_id` in policy snapshot.)
- **Cost forensics:** *"Tell me which agent in this run burned 60% of the budget."* (`agents[].llm_cost_usd` sums per node.)
- **Family attribution audit:** *"Confirm this agent ran on the family the recipe promised."* (`agents[].family` vs `agents[].model_lane` vs actual `llm_calls[].provider` — the load-bearing receipt for the heterogeneity claim.)
- **Prompt-vs-output diff:** *"Show me the exact prompt this agent saw and the exact markdown it emitted side by side."* (`agent_detail.prompt.content` next to `agent_detail.artifacts[]`.)

### 16b. New routes the SPA must add

| Path | Page | Purpose |
|---|---|---|
| `/runs/:taskRunId/agents` | new tab on `RunDetailPage` **or** a new top-level route | Agent list per run — table-or-tree of every recipe node with status, family, $, latency, LLM count |
| `/runs/:taskRunId/agents/:nodeId` | new sub-route | Per-agent forensics: prompt, artifacts, LLM calls, child spawns |
| `/runs/:taskRunId/lineage` (optional) | new tab | Recursive tree across `run_spawns` — agents nested under their parent runs |

**Recommended IA:** add an **Agents** tab to the existing tab strip on `RunDetailPage` (between *DAG* and *Artifacts*), and treat the per-agent view as a drawer/side-panel on the same route rather than a navigation push. Reasons:

- The DAG tab already renders the structural view (xyflow); the Agents tab adds the *runtime* view of those same nodes.
- A drawer keeps the operator one click from comparing two agents in the same run (drawer A → close → drawer B). A navigation push forces a back trip.
- The lineage view (recursive sub-runs) is a third tab — most runs have zero children, so it should be conditional ("Lineage (3)" only when `children.length > 0`).

If the designer prefers a dedicated route, the breadcrumb `fleet / <task_run_id> / agents / <node_id>` is still cheap.

### 16c. Data the new view gets (already shipped)

**`GET /api/v1/task-runs/:taskRunId/agents`** → `{ task_run, recipe, edges, agents[], children[] }`

Each `agents[]` row carries:

- Identity: `node_id`, `node_type`, `model_lane`, `family`, `prompt_ref`, `verifier_ref`, `dispatch_mode`, `gates[]`
- Runtime: `status` (`never_seen` / `running` / `done` / `failed`), `duration_ms`, `verdict`, `started_at`, `ended_at`
- Artifacts on disk: `artifact_files[]` (basenames)
- LLM rollup: `llm_call_count`, `llm_cost_usd`, `llm_total_tokens`, **`llm_attribution_fallback` (bool)** — true when the call couldn't be attributed via `metadata_json.node_id` and was matched by time-window. The UI must badge fallback-attributed rollups as "approximate" the same way the events table flags `bridge=time-window` rows.

`children[]` (from `run_spawns`) carries: `spawn_id`, `child_run_id`, `depth`, `recipe`, `kickoff_path`, `authority_level`, `allow_child_spawn`, `status`, `created_at`. Each child opens a new RunDetailPage (`/runs/<child_run_id>`) — the recursion is just URL navigation.

**`GET /api/v1/task-runs/:taskRunId/agents/:nodeId`** → `{ task_run_id, node, status, duration_ms, verdict, started_at, ended_at, prompt: { path, content }, artifacts[], llm_calls[], children[] }`

`llm_calls[]` rows include the **failure-relevant fields the existing LLM tab doesn't show today**:

- `error_message` — the provider's literal error string when a call failed
- `finish_reason` — `stop` / `length` / `content_filter` / etc.
- `traceparent` — the full W3C trace context, useful for cross-call lineage
- `metadata_json` — provider-side metadata blob; includes `node_id` when the dispatcher attributed correctly

### 16d. IA — the Agents tab

```
RunDetailPage tab strip:
  Overview · DAG · Agents [N] · Artifacts · Events · LLM calls · Lineage (M)
                    └─────────────────────┐
                                          │
   Section A: Per-agent rollup table      │
   ┌──────────────────────────────────────┴────────────────────────────┐
   │ node_id            family    status   $       tokens   ms    LLM │
   │ ─────────────────  ────────  ──────   ─────   ──────   ───   ─── │
   │ ▶ perf_lens        glm       done     $0.12   12.3K   8 421  4   │
   │ ▶ security_lens    kimi      done     $0.09    9.8K   7 152  3   │
   │ ▶ codex_lens       codex     failed   $0.05    3.2K   2 901  1 ⚠ │
   │ ▶ opus_lens        opus      done     $0.21   15.1K  10 333  5   │
   │ ▶ synthesizer      sonnet    done     $0.18   18.0K   9 014  2   │
   │ ▶ verifier         —         done       —       —    1 200  0   │
   └────────────────────────────────────────────────────────────────────┘

   Filter chips: [all] [failed] [running] [family: glm|kimi|...] [⚠ fallback attribution]

   Sort affordances: $ desc default (cost-forensics JTBD) — click headers to re-sort

Drawer (right slide-in, ~520 px wide) opens on row click:
   ┌── PERF_LENS · glm · done ────────────────── × ─┐
   │ 8.4 s · $0.12 · 4 LLM calls · verdict APPROVE  │
   │ ─ tabs: Prompt · Output · LLM calls · Children │
   │                                                │
   │ [Prompt tab]                                   │
   │   path: recipes/refactor-audit/prompts/...     │
   │   <syntax-highlighted markdown>                │
   │                                                │
   │ [Output tab]                                   │
   │   lens-perf.md (8.2 KB)                        │
   │   <react-markdown render>                      │
   │                                                │
   │ [LLM calls tab]                                │
   │   ts          model   $        tokens  status │
   │   ──────────────────────────────────────────  │
   │   2 m ago     z/glm   $0.04    3.2K   ✓ stop │
   │   2 m ago     z/glm   $0.03    2.8K   ✓ stop │
   │   1 m ago     z/glm   $0.05    4.1K   ✗ len  │
   │              ↑ click row → expand error_msg   │
   │                                                │
   │ [Children tab] (only if children.length > 0)   │
   │   spawn_id  recipe          child_run_id  →   │
   │   ─────────────────────────────────────────   │
   │   sp-… ...  research-synth  tr-… ...     [↗] │
   └────────────────────────────────────────────────┘
```

### 16e. State machine — Agents tab

Per the agent rollup:

| State | Visual treatment |
|---|---|
| `never_seen` | Row dimmed, `—` in metric cells, "pending" pill in `ork.slate` |
| `running` | Pulsing dot in status cell, `—` for ended-at, live-updating duration_ms from `now - started_at`, SSE-refresh on `node_end` |
| `done` | Standard row, `verdict=APPROVE` pill in `ork.green` |
| `done` with non-APPROVE verdict | Yellow row left-border (`ork.amber`), verdict pill matches verdict color |
| `failed` | Red row left-border (`ork.red`), AlertTriangle prefix, failed-LLM-call count badge if `llm_calls[].status=failed` exists |
| `llm_attribution_fallback=true` | Small ⚠ icon next to `LLM` column header value with tooltip: "LLM cost shown is approximate — calls were matched by time-window because metadata_json.node_id was missing" |

Drawer states:

| State | Behavior |
|---|---|
| Cold | Skeleton in prompt + LLM list |
| Prompt missing on disk | "Prompt file not found at `<expected path>` — recipe may have used dynamic prompt assembly" |
| Output artifacts empty | "No output artifacts on disk yet. Agent may still be running, or recipe may not produce a file under expected names: `lens-<id>.md`, `verifier-<id>.log`, …" |
| LLM calls empty + `bridge=trace_id` available | "No LLM calls correlated to this agent. The dispatch may have used a tool other than the standard `lib/llm-dispatch.sh`." |
| LLM calls present, some failed | Failed rows expanded by default; `error_message` rendered in monospace inside the expanded row |
| Children empty | Tab hidden (do not show "Lineage (0)") |

### 16f. Lineage (recursive sub-runs) tab

The third new tab. Shows the recursive shape of nested mini-ork runs (when an agent spawned a sub-run via `bin/mini-ork-spawn`).

Render as an **indented list-tree**, not a graph — the depth ladder makes ancestry trivial to read:

```
this run  (tr-1780411…)
  ├─ spawned by  synthesizer
  │   ├─ child  research-synth  tr-1780411b…  status=done    $0.31  18 s
  │   └─ child  refactor-audit  tr-1780411c…  status=running  …    [open]
  └─ spawned by  opus_lens
      └─ child  recursive-self-improve  tr-1780411d…  status=failed  $0.04  3 s [why?]
```

Each row links to the child's RunDetailPage. The "[why?]" affordance on failed children jumps the user to that run's Overview tab with WhyCard already expanded.

Depth limit for in-line render: 2. Deeper trees collapse to "+N descendants" with a "expand" affordance — recursion can be unbounded in self-improve loops.

### 16g. Visual identity for agents

The Agents tab is where the framework's brand thesis becomes legible: heterogeneity is the product. Designer should resist the urge to homogenize the rows visually. Instead:

- Bring forward the family color: every row's leftmost ~3 px stripe in `familyColor(family)` (already implemented in `web/src/lib/format.ts`). Reader scans the column and *sees* coalition risk at a glance.
- Family pill in the row uses `FamilyPill` (already shipped) — keep it; don't recolor.
- For `failed` rows, the family stripe stays but the row gains a soft `ork.red/8` background tint so failure dominates color, not family.

This produces a small but real second-order benefit: the brand thesis becomes a navigation affordance. *"All my failures are in the orange stripe"* → operator now knows GLM is sandbagging on this recipe.

### 16h. Out of scope (for this section)

- **Live LLM call streaming** (token-by-token). Backend has no `llm_call_chunks` table. Future work.
- **Re-running an agent in isolation.** That's the control track (§14 L2/L3), not transparency.
- **Cross-run agent comparison** ("show me how `perf_lens` did across the last 10 runs"). Excellent epic, but not what the user asked for here. Track as a follow-on.

### 16i. Acceptance test (for designer + dev)

After implementation, an operator should be able to:

1. Open `/runs/<some_failed_task_run_id>` cold
2. Click the **Agents** tab
3. See exactly one row with a red left-border, ⚠ failed pill, and a non-zero "failed LLM calls" badge
4. Click that row → drawer opens
5. Click **LLM calls** tab in the drawer → see the failed call expanded with `error_message` visible
6. Total clicks: **3** (Agents tab → row → LLM calls tab). Zero terminal switches.

If the designer can't make 6-step traversal feel native in their mock, redesign.

---

## 17. Cohesive UX — global IA + Overview page (NEW — supersedes §7 sidebar)

> **User request, verbatim (2026-06-09):**
> *"We need a cohesive user experience. In the main page user can see an overview of mini-ork runs, trajectories, learnings, etc. On the nav side, we first should have a menu showing all mini-ork runs and then when clicking on each it should show the full info on that mini-ork including its main artifacts, when started, its status, etc. Other options on the main left navbar should be based on all other features we provide."*

This section **replaces the global IA in §7** (which described the as-shipped flat 3-item sidebar). §7's per-page IA is still authoritative; only the cross-page navigation model changes.

### 17a. Sidebar — new structure

```
┌─ mini-ork ────────────────┐
│  mO observability         │
├───────────────────────────┤
│  ★ Overview               │  /            home dashboard (was "Fleet")
│  ▼ Runs                 N │  /runs        expandable list (counts active runs)
│      🔍 search runs…      │
│      ─────────────────    │
│      ● tr-1780411…  exec  │  /runs/<id>   per-run forensics (see §7 + §16)
│      ● tr-1780410…  done  │
│      ● tr-1780409…  fail  │
│      ─────────────────    │
│      View all (N)  →      │  /runs        full-list page (filterable table)
│                           │
│  ▼ Trajectory             │  /trajectory  cost + wall-time + convergence
│      Self-improve loops   │  /trajectory/self-improve
│      Cost / day           │  /trajectory/cost
│      Wall time            │  /trajectory/wall-time
│                           │
│  ◆ Fingerprint            │  /fingerprint  heterogeneity receipts (brand hero)
│                           │
│  ▶ Learnings              │  /learnings   persistent docs (audits, improvements, research, operator notes, blog drafts)
│                           │
├───────────────────────────┤
│  state.db connected       │  health pill (unchanged)
│  .mini-ork/state.db       │
└───────────────────────────┘
```

**Section ordering rationale:**

1. **Overview first** — the JTBD "what's happening right now" lands you on the synthesis screen, not a raw table.
2. **Runs second, expandable** — the user said this explicitly. Runs are the central object of the framework; everything else is a lens on them.
3. **Trajectory + Fingerprint** — cross-run views. These are the "framework is improving" + "framework is heterogeneous" receipts respectively.
4. **Learnings last** — persistent knowledge artifacts. Slow-moving, high-value-when-needed.

Sidebar width stays 14rem (`w-56`) for the default state; widens to 18rem when **Runs** is expanded to accommodate the search field + status pills. Designer chooses the visual disclosure (chevron + slide vs. accordion vs. tray).

### 17b. The expandable Runs section in the sidebar

This is the design's most opinionated move. Patterns to follow:

| Concern | Recommended treatment |
|---|---|
| **List density** | Show the 12–20 most-recent runs in the sidebar; longer list lives at `/runs`. The sidebar is a "jump to recent" affordance, not a full registry. |
| **Each row** | `● <id-short>  <recipe-short>  <status>` where `●` = small family-colored dot, `<id-short>` = first 12 chars of `task_run_id` in mono, `<recipe-short>` = abbreviated recipe (`refactor-audit` → `r-audit`), `<status>` = compact pill |
| **Active runs pinned** | `runs/active` returned items sort to top of the list, with a subtle "live" indicator (pulsing dot). Recent finished runs follow chronologically. |
| **Search** | Sticky input at top of expanded section. Filters by `id` prefix or `recipe` substring. Powered client-side over the cached `taskRuns({limit: 200})` list — no new endpoint needed. |
| **Empty / no-runs state** | "No runs yet. Try `mini-ork classify <kickoff>`." linked to README. |
| **Loading state** | 5 skeleton rows. |
| **Active-run highlighting** | When user is on `/runs/<id>`, the matching sidebar row gets a left-edge `ork-amber` indicator + bold ID. Match TanStack Router's `activeProps`. |
| **Right-click context (stretch)** | "Open in new tab" — useful for comparing two runs side by side. |
| **"View all" affordance** | Bottom of expanded section: `View all (N) →` jumps to a paginated `/runs` page with filters (recipe, status, verdict, cost range, date range). This page is currently *the existing FleetPage's tables, demoted* — it stays useful but is no longer the home page. |

**Why a sidebar list instead of a top tabbar:** the user wants to *navigate from each run* and have *info on each run* surface immediately. A flat sidebar matches the operator mental model ("my runs are these things on the left") more naturally than a top-tabbar that requires modal selection. It also reuses the muscle memory from VS Code's Source Control + Run panels.

### 17c. Overview page (the new `/`)

Replaces today's FleetPage-as-tables. The Overview is **a synthesis dashboard**, not a registry. It must read as one coherent screen, not four disconnected widgets.

Proposed composition (designer can rearrange — these are the required content blocks, not the literal layout):

```
┌─────────────────────────────────────────────────────────────────────┐
│  Header strip                                                       │
│   "5 runs in flight · 2 failed in 24h · $12.34 spent today"        │
│   (clickable phrase fragments — each links to filtered /runs view)  │
├─────────────────────────────────────────────────────────────────────┤
│  Hero: heterogeneity receipt                                        │
│   The currently-active recipe's coalition badge + family histogram  │
│   Mirrors the Fingerprint page hero; one-click expansion            │
├─────────────────────────────────────────────────────────────────────┤
│  Live: active runs strip                                            │
│   Horizontal cards, one per active run, with mini-DAG progress      │
│   + cost-spent / cost-cap meter + "open" affordance                 │
├─────────────────────────────────────────────────────────────────────┤
│  Trajectory snapshot                                                │
│   7-day cost-by-day sparkline + 7-day convergence ledger summary    │
│   (e.g. "iter 30→33 — 4 partial, 2 converged, 1 aborted")          │
├─────────────────────────────────────────────────────────────────────┤
│  Recent learnings                                                   │
│   Last 5 entries from docs/audits/, docs/improvements/, docs/...    │
│   Each row: title · folder · date · "open" → /learnings/<slug>      │
├─────────────────────────────────────────────────────────────────────┤
│  Footer affordance                                                  │
│   "Want the old fleet view? View all runs →" → /runs                │
└─────────────────────────────────────────────────────────────────────┘
```

**Block-by-block rationale:**

- **Header strip** is the headline: a one-line sentence summarising state. Reading time < 3 s. Inspired by GitHub's repo-level "5 contributors · 12 issues · last update 2h ago" header.
- **Hero (heterogeneity receipt)** elevates the brand thesis to the homepage. If you only show one card on the home, this is it. The Fingerprint page becomes the deep-dive; the Overview shows the **currently-relevant** receipt — which is whichever recipe the most-recent run used.
- **Active runs strip** is the operational pulse. Cards over a table because cards make "click to open" obvious and let us fit a mini-DAG progress bar in each.
- **Trajectory snapshot** is the "framework is improving" claim, miniaturized. Two small charts maximum; the Trajectory page is for power-users.
- **Recent learnings** ties the doc artifacts to the dashboard. Currently the framework writes durable findings to `docs/audits/`, `docs/improvements/`, `docs/research/`, `docs/operator/`, `docs/blog/` — operators have no UI surface to discover what was learned last week. The Overview is the place to fix that.

### 17d. New top-level routes

| Route | Purpose | Status |
|---|---|---|
| `/` | Overview synthesis dashboard (this page) | redesign — replaces FleetPage |
| `/runs` | Paginated, filterable run registry | new — repurposes today's FleetPage tables |
| `/runs/:taskRunId` | Per-run forensics (Overview / DAG / Agents / Artifacts / Events / LLM / Lineage) | extend per §16 |
| `/trajectory` | Existing | unchanged structurally; designer applies overview-page voice |
| `/trajectory/self-improve/:runId` | Existing | unchanged |
| `/fingerprint` | Existing brand hero | unchanged structurally |
| `/learnings` (NEW) | Document explorer: lists `docs/audits/*.md`, `docs/improvements/*.md`, `docs/research/*.md`, `docs/operator/*.md`, `docs/blog/*.md` | new — needs a backend endpoint (likely `/api/v1/learnings`) and a markdown viewer page |
| `/learnings/:slug` | Renders a single markdown doc inline | new |

Note: `/learnings` is a NEW backend epic — there is currently no endpoint serving these docs. The brief calls for the route + UX, and flags the dependency in §17g.

### 17e. State machine — Overview page

| State | Behavior |
|---|---|
| Cold | All 5 blocks render skeletons. Total skeleton time should not exceed 1.5 s. |
| No runs at all | Hero replaced with empty-state CTA: "Welcome to mini-ork. Try `mini-ork classify <your kickoff>.md`." with code snippet + link to README. Other blocks hidden. |
| Runs exist but none active | Active-runs strip says "No runs in flight. Last completed: `<run_id>` <ago>" linking to that run. |
| All blocks loaded, healthy | Live-updates indicator in header (1s SSE refresh on `state.db` changes). |
| `state.db` unreachable | Top-level red banner: "state.db unreachable. Check `mini-ork serve` is running and `--home` points at the right `.mini-ork/`." All blocks gray out. |

### 17f. Cohesion — what "cohesive UX" means here

The user emphasized *cohesive*. Designer should enforce four cross-page conventions:

1. **One status vocabulary.** `StatusPill` and `VerdictPill` already exist (`web/src/components/Pill.tsx`). Every place a status appears uses the same pill component, same color, same icon. No bespoke status badges per page.
2. **One time treatment.** `formatRelative` for everything ≤ 24 h old; absolute ISO date for older. No mixing "2h ago" with "yesterday at 4pm".
3. **One family color.** `familyColor()` from `web/src/lib/format.ts` is the single source of truth. Sidebar dot, DAG node border, agent row stripe, FamilyPill all read from it.
4. **One nav primitive.** Sidebar is the only nav primitive. No top-tab navigation (the per-page tab strips on RunDetailPage are *within* a route, not cross-page navigation). No floating action buttons.

### 17g. Dependencies + risks

- **`/learnings` backend.** New FastAPI endpoint required. Cheapest design: list all `*.md` under `docs/{audits,improvements,research,operator,blog}/` with frontmatter parsing for title + date, plus a path-safe content reader. Designer should mock the UX assuming it exists; engineering will land it in parallel.
- **Sidebar runs list scales poorly past ~5000 runs.** Today's repo has tens. By the time we have thousands, the sidebar should switch to "active + pinned + recent 10" with the registry at `/runs` carrying the load. Designer should mock both small-N (today) and large-N (future) sidebar states.
- **Search performance.** Client-side filter over a `taskRuns({limit: 200})` cache is fine until the dataset breaks 200 rows. Future epic: server-side search endpoint.
- **The "active recipe" for Overview hero.** Picking which recipe's fingerprint appears at the top is a heuristic — "the recipe of the most recent active run, falling back to the recipe of the most recent task_run". Document this in the mock so engineering doesn't reinvent.

### 17h. What this means for §7

§7's per-page IA is preserved. The cross-page IA in §7's first diagram is **superseded by §17a**. When the designer reads the brief end-to-end, §17 wins over §7 on global nav, and §7 wins over §17 on per-page composition. (Future editorial pass: collapse §7's global-nav into a back-reference to §17.)

---

## 18. Call inspector — every LLM call + tool call, Claude Code style

> **User request, verbatim (2026-06-09):**
> *"In a proper part of the UI I should be able to see every single call the agent had in a beautiful way, also for trajectories. We'll be adding it soon but user should be able to see each tool call, the output, etc. — similar to how Claude Code shows these info."*

### 18a. Schema reality (today vs. soon)

**Today's `llm_calls` table stores no message content.** Columns: `provider, model_id, tier, feature_name, actor, input_tokens, output_tokens, total_tokens, cost_usd, duration_ms, status, finish_reason, error_message, traceparent, metadata_json, ts`. That's metering, not transcripts.

Failed calls do dump payloads to `.mini-ork/runs/<task_run_id>/llm-failures/<ts>-<provider>.{out, err.log, shim.err}` — but successful calls leave no body on disk or in the substrate.

**Coming soon (per the user, scope to be confirmed with PM):**

- Per-call request body (the message list sent to the provider) — assumed shape: `messages: [{role, content}]`
- Per-call response (assistant message + any tool-use blocks)
- Per-call `tool_calls[]` — each with `name`, `input` (args), `output` (result), `status`, `duration_ms`, `is_error`
- Optionally: streamed token chunks for live tail (out of scope for v1)

Concrete recommendation to engineering: append two columns to `llm_calls` *or* write per-call jsonl to `.mini-ork/runs/<task_run_id>/llm/<call_id>.jsonl` and add an endpoint:

```
GET /api/v1/task-runs/:taskRunId/llm-calls/:callId/transcript
  → { messages: [{role, content}], response: {...}, tool_calls: [{name, input, output, status, duration_ms, is_error}] }
```

Designer should mock against **this assumed shape**. If engineering ships a different shape, the mocks adapt — the visual language survives.

### 18b. The Claude Code visual language (what to borrow, what to drop)

Claude Code's call inspector works because of five specific design moves. Borrow these:

1. **Compact one-line cards by default.** Each tool call is a single horizontal row with a tool-icon prefix, tool name in monospace, args summary (truncated), and a small status icon at the right edge. Reader can scan 20+ calls without scrolling each.
2. **Click-to-expand without navigation.** Expansion happens in-place — the card grows down. No route push, no modal. Preserves the operator's place in the conversation flow.
3. **Status as the dominant visual.** A failed `Bash` call has a red left edge and a red icon that's visible at a glance even when collapsed. Color does the work; copy is secondary.
4. **Diff treatment for `Edit` / `Write` calls.** When the tool output is "I changed these lines," the inspector shows actual unified diff with green/red gutters — not "the file was modified."
5. **Outputs preserve their nature.** Shell output is monospaced + dark background. JSON is syntax-highlighted. Markdown is rendered. The inspector reads the tool's name to pick the renderer.

Drop these from Claude Code's pattern:

- The thinking-block treatment (we don't have it, and the framework doesn't surface it).
- The "Run in your terminal" CTA (irrelevant — these calls already ran).
- The terminal-prompt bash-prefix styling for headers — too noisy for a dashboard chrome.

### 18c. Where the inspector lives

Two surfaces, **same component, different scope**:

| Surface | Scope | Entry point |
|---|---|---|
| **Per-agent call inspector** | All LLM calls + tool calls for one agent in one run | Drawer's "LLM calls" tab (already specified in §16d) — the placeholder table becomes the full inspector |
| **Per-trajectory call inspector** | All LLM calls + tool calls across every iteration of a self-improve loop, in chronological order, grouped by iter | New `/trajectory/self-improve/:runId/calls` route, OR a "Calls" tab added to the existing `SelfImproveDetailPage` |

Recommended: add **Calls** as a tab on `SelfImproveDetailPage` to keep the iteration context intact. The trajectory page (`/trajectory`) gets a top-level "All calls" link only if it tests well — most operators arrive via a specific iter, not the cross-loop view.

### 18d. Component spec — `<CallInspector />`

```
Layout (collapsed row, ~32 px tall):

┌──────────────────────────────────────────────────────────────────────────┐
│ ┃ 🔨 Bash    grep -rn "TODO" src/      ✓ 142ms · 4.1KB out      ▶ │
└──────────────────────────────────────────────────────────────────────────┘
  ↑   ↑       ↑                          ↑                         ↑
  │   │       │                          │                         expand chevron
  │   │       │                          status + perf (right-aligned)
  │   │       args summary (1 line, truncated)
  │   tool name (monospace, primary text)
  family-color stripe (3 px, left edge — § 16g cohesion)

Layout (expanded, ~auto-height):

┌──────────────────────────────────────────────────────────────────────────┐
│ ┃ 🔨 Bash                                            ✓ 142ms · 4.1KB ▼ │
│ ┃                                                                       │
│ ┃ ── Input ──────────────────────────────────────                       │
│ ┃ grep -rn "TODO" src/                                                  │
│ ┃                                                                       │
│ ┃ ── Output (4.1 KB, 87 lines) ──────────────────         [copy] [↗]  │
│ ┃ src/auth.ts:12:  // TODO: rate limit                                  │
│ ┃ src/auth.ts:34:  // TODO: add OAuth                                   │
│ ┃ src/api.ts:8:    // TODO: cache invalidation                          │
│ ┃ … 84 more lines ▼ show all                                            │
└──────────────────────────────────────────────────────────────────────────┘
```

**Card variations by tool type:**

| Tool | Renderer | Notes |
|---|---|---|
| `Read` | Plain text (truncated) with line numbers | Show first 30 lines; "show all (N more)" expander |
| `Write` / `Edit` | Unified diff with green/red gutters | Use `react-diff-viewer` or similar; no new dep required if we hand-roll |
| `Bash` | Monospace dark block | stdout + stderr in two stacked panes; exit code badge |
| `Grep` | Result list, one match per line, file path link | Click line → opens `Read` view of that file |
| `Glob` | File list with sizes + mtimes | Tabular |
| Network / `WebFetch` | URL + status + response preview | Headers collapsible |
| Unknown tool | Generic JSON pretty-print | Fallback safety net |
| Provider call itself (the LLM message exchange) | Chat-style cards: `user` left-aligned, `assistant` right-aligned, `tool_result` inline beneath the tool call | This is the "messages" view, distinct from individual tool calls |

**Tool call lifecycle states** (border + icon):

| State | Border color | Icon | Notes |
|---|---|---|---|
| `pending` | `ink-700` | ⏳ | Tool selected by the LLM but not yet executed (rare to surface for replay; relevant for live tail) |
| `running` | `ork-amber` (pulsing) | ⟳ | Live tail only |
| `completed` | `ink-700` (no accent) | ✓ in `ork-green` | Normal state |
| `completed` with non-empty `error_message` | `ork-red` | ⚠ in `ork-red` | The tool ran but returned an error |
| `failed` (tool didn't run) | `ork-red` (solid 3 px) | ✗ in `ork-red` | Shim error, permission denied, etc. |
| `truncated` (output exceeded limit) | `ork-amber` | ↘ | Tag with "output truncated at N KB" |

### 18e. Conversation view (LLM message exchange, not just tool calls)

The user said "every single call the agent had" — that includes the *LLM message exchange*, not just the tool subtree. So the inspector has **two layers**:

1. **Conversation layer** — the chronological sequence of messages between the agent and the model:
   - `user` (or "system") message: the prompt
   - `assistant` message: the model's text + tool-use intents
   - `tool_result` message: what the tool returned

2. **Tool call layer (sub-cards)** — tool calls nested under the assistant message that triggered them, with their output as the corresponding `tool_result`.

Visual model:

```
┌─ Message 1 · user · 2,341 tok ───────────────────────────────────────┐
│ <prompt content, markdown-rendered>                                  │
└──────────────────────────────────────────────────────────────────────┘

┌─ Message 2 · assistant · Sonnet 4.6 · 412 tok · 1.2 s ──────────────┐
│ I'll start by searching for the TODOs in src/.                       │
│                                                                      │
│ ┌─ Tool call · Bash · 142 ms ────────────────────────────────────┐ │
│ │ grep -rn "TODO" src/                                             │ │
│ │ → 87 matches                                                     │ │
│ └──────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘

┌─ Message 3 · tool_result · 87 matches ──────────────────────────────┐
│ <output rendered as Grep result list — see 18d>                      │
└──────────────────────────────────────────────────────────────────────┘

┌─ Message 4 · assistant · Sonnet 4.6 · 1,231 tok · 3.4 s ────────────┐
│ Found 87 TODOs across 12 files. The hotspot is src/auth.ts (34 ).    │
│ Let me read it to categorise them.                                   │
│                                                                      │
│ ┌─ Tool call · Read · 9 ms ───────────────────────────────────────┐ │
│ │ src/auth.ts                                                      │ │
│ └──────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
   … continues …
```

Message cards use a left-edge accent strip mirroring the role:

- `user` / `system` → `ink-500` (neutral)
- `assistant` → `familyColor(call.provider)` — the family-color cohesion rule (§16g, §17f) applies here
- `tool_result` → `ink-700` (deemphasized — it's the consequence of the prior assistant action)

### 18f. Density modes

Operators inspect differently when scanning vs. debugging. Three modes via a control in the inspector header:

| Mode | Default for | Behavior |
|---|---|---|
| **Compact** | Per-trajectory view (many iters, many calls) | All cards collapsed; one row per message; tool calls inline as pills (`Bash` `Read` `Edit`) |
| **Standard** | Per-agent view | Cards collapsed by default; failed and error-flagged calls auto-expanded; meaningful previews |
| **Verbose** | "I'm debugging this one call" | Everything expanded, all outputs full-length, raw `metadata_json` shown in a "details" footer |

Persist the mode in `localStorage` per surface (`call_inspector_density_agent` / `_trajectory`).

### 18g. Filtering, search, scope controls

Inspector header includes a control bar:

```
[Density: compact ▾]  [Filter: ⬚ failed ⬚ ⚠ error  by tool ▾  by model ▾]  [🔍 search…]  N calls · $0.42 · 14.3 s
```

- **Filter chips** are toggleable. Default off. When `failed` is on, all green calls hide.
- **Tool filter** is a multi-select dropdown populated from the distinct tool names in scope.
- **Model filter** is a multi-select populated from distinct `(provider, model_id)` pairs.
- **Search** is substring across tool name, args summary, output text. Highlights matches inside expanded cards.
- **Right edge** shows the **scope rollup**: total call count + total cost + total wall time across the currently-filtered set. The number updates as filters apply.

For the trajectory view, add one more chip: **`by iter`** — multi-select of iteration numbers. Default all iters in scope.

### 18h. Trajectory-specific concerns

Showing every call across a 33-iteration self-improve loop is a lot. The trajectory inspector needs:

- **Default scope: last 3 iterations.** "Show all (33 iters)" expands.
- **Iter delimiters.** Each iteration's calls are visually grouped under a sticky `iter N — outcome — duration` header that stays pinned as the user scrolls.
- **Iter mini-timeline** at the top: 33 small bars colored by outcome (green/amber/red); clicking a bar jumps to that iter's calls.
- **Cross-iter aggregates** in the header: "33 iters · 412 calls · $4.87 · avg 9 s/iter."
- **Call-count anomalies highlighted.** If iter 17 made 4× the calls of the median iter, its delimiter shows a small ⚠ "anomalous volume" pill — designer chooses the exact threshold and copy.

### 18i. Edge cases

| Case | Treatment |
|---|---|
| **Successful call, transcript missing (legacy data)** | Card shows the metadata-only state with a muted note: "Call body not stored. See `llm-failures/` for failed-call payloads, or rerun after the v2 schema lands." |
| **Failed call, only failure-log on disk** | Mount the file from `.mini-ork/runs/<task_run_id>/llm-failures/<ts>-<provider>.err.log` as the "Output" pane. Add a "raw failure log" disclosure for `.shim.err`. |
| **Truncated output** | Show first 30 lines + "show all (N more, K KB)" expander. Above 1 MB, force "download" instead of inline. |
| **Streaming (future)** | Out of scope for v1; design with a placeholder for the future "live" indicator. |
| **Multi-part content (image + text)** | Render images inline; text follows. (Not in scope unless multimodal lanes are added — flag in the mock.) |
| **PII / secrets in output** | No automatic redaction; surface a small "view raw" affordance that requires a deliberate click. (Loopback-only posture — operators are the only audience today.) |
| **Tool name not in the renderer registry** | Fallback to JSON pretty-print of `tool_call.input` and a monospace `tool_result.output`. Show a small "unknown renderer" tag so engineering knows to extend. |
| **Very long conversation (100+ messages)** | Virtualized list (react-virtual / TanStack Virtual). Designer should plan for this — a 33-iter self-improve loop can produce thousands of messages. |

### 18j. Backend dependency + mocking

- The visual language can be designed **today** against fixture data — designer should generate 1–2 sample conversation JSON files matching the assumed shape in §18a.
- Engineering's dependency: extend `llm_calls` (or write per-call jsonl to disk) + add the `/transcript` endpoint. Track as a separate epic.
- Until the endpoint lands, the per-agent drawer's "LLM calls" tab continues to show the metadata-only table (today's content). The inspector is rolled out the moment the endpoint exists; the rest of the brief is unblocked.

### 18k. Success criteria

The inspector is "beautiful" enough when:

1. An operator can land on a failed agent's drawer, expand the inspector, and read the exact tool-call that broke with zero scrolling past unrelated noise. (Standard density auto-expands failed calls — §18f.)
2. Scanning a 412-call trajectory feels like scanning a chat thread, not reading a logfile. Compact mode + iter delimiters + sticky headers carry this.
3. The render fidelity of an `Edit` diff is good enough that an operator can spot-check the change without opening the file. (Unified diff with gutters — §18d.)
4. Family-color cohesion holds: the inspector reads as the same product as the rest of the dashboard, not as a borrowed widget.

---

## Appendix A — API surface today (for reference)

All GET, all under `/api/v1`. Source of truth: `app.routes` (the self-documenting `/api` endpoint emits this list at runtime).

| Endpoint | Returns | Used by |
|---|---|---|
| `/health` | `{ ok, db_path, has_task_runs, has_mo_events, has_self_improve_runs }` | Shell sidebar pill |
| `/runs/active` | `ActiveRun[]` | Fleet active table |
| `/task-runs?limit=&recipe=&status=&verdict=` | `TaskRun[]` | Fleet recent table |
| `/task-runs/summary` | `{ by_recipe, by_status, total_cost_usd }` | Fleet stat cards |
| `/task-runs/:id` | `TaskRun` | RunDetail metadata grid |
| `/task-runs/:id/why` | `Diagnostic` | RunDetail WhyCard |
| `/task-runs/:id/evidence?path=` | `EvidenceContent` | WhyCard expanded evidence |
| `/task-runs/:id/correlation` | `Correlation` | RunDetail CorrelationCard |
| `/task-runs/:id/events?limit=` | `RunEvent[]` (mo_events ∪ run_events, bridge-tagged) | RunDetail Events tab |
| `/task-runs/:id/llm-calls` | `LlmCall[]` (trace_id strict + time-window fallback) | RunDetail LLM tab |
| `/task-runs/:id/artifacts` | `ArtifactEntry[]` | Artifacts tab tree |
| `/task-runs/:id/artifacts/:relpath` | `ArtifactContent` | Artifact viewer |
| `/task-runs/:id/dag` | `Fingerprint + node statuses` | DAG tab |
| **`/task-runs/:id/agents` (NEW)** | `{ task_run, recipe, edges, agents[], children[] }` — see §16c | **Agents tab (to build)** |
| **`/task-runs/:id/agents/:node_id` (NEW)** | `{ node, status, prompt:{path,content}, artifacts[], llm_calls[], children[] }` — see §16c | **Agent drawer (to build)** |
| `/trajectory/self-improve?limit=` | `SelfImproveRun[]` | Trajectory ledger |
| `/trajectory/self-improve/:runId` | `SelfImproveDetail` | SelfImprove detail |
| `/trajectory/cost-by-day` | `CostByDayRow[]` | Trajectory chart |
| `/trajectory/wall-time` | `WallTimeRow[]` | Trajectory chart |
| `/fingerprint?recipe=` | `Fingerprint` | Fingerprint page hero |
| `/fingerprint/recipes` | `string[]` | Recipe selector |
| `/stream?task_run=` | SSE | RunDetail live invalidation |

## Appendix B — Designer setup (one-shot)

```bash
# Boot backend
make test-web                      # sanity-check smoke tests pass first
mini-ork serve --reload            # :7090, hot-reload Python

# Boot SPA in another terminal
pnpm --dir web install
pnpm --dir web dev                 # :7070 (Vite), proxies /api → :7090

# Smoke-data
# Repo's own .mini-ork/state.db has 33+ self_improve_runs, dozens of task_runs.
# Designer can mock additional rows by writing SQLite directly if needed —
# `state.db` is committed to .gitignore so local mutations are safe.
```
