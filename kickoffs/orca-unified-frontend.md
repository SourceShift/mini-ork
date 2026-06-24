# Kickoff: Orca as unified frontend over mini-ork + ContextNest (orca-unified-frontend)

## Goal

Make **Orca** (stablyai/orca — Electron desktop + mobile AI agent orchestrator)
the **frontend UI** for the two headless backends that already exist:

- **mini-ork** — the BRAIN: multi-LLM recipe DAGs + the learning loop
  (`execution_traces`, `gradient_records`, `prompt_win_rates`,
  `agent_performance_memory.relative_advantage`, `idea_tree_nodes`).
- **ContextNest (CN)** — the MEMORY substrate, an independent HTTP service on
  `CN_BASE_URL` (default `http://127.0.0.1:28080`), 12 REST endpoints.

Orca owns **no** learning math and **no** memory substrate. It is a
**read-render + thin-command + lifecycle-writer**: it reads mini-ork's SQLite
and CN's REST, kicks off mini-ork runs as commands, and — because it now spawns
the agents — becomes the producer of CN hook/outcome events and trace rows.

The centerpiece deliverable is the **Run Trajectory view**: for a single
`run_id`, stitch the closed loop *CN atoms injected → recipe DAG / idea-tree
nodes (each a worktree) → per-node execution_traces + verdicts + cost →
gradients extracted (joined by `evidence = trace_id`) → outcome posted to CN →
confidence delta*. Neither tool can show this today: mini-ork has the data and
no eyes; Orca has eyes and no data.

**Target repo (where the code lands):** the Orca checkout. Implementers MUST
operate on a working copy of `git@github.com:stablyai/orca.git` on a feature
branch (e.g. `feat/mini-ork-cn-frontend`), NOT on the mini-ork repo. mini-ork is
read-only backend here.

## Architectural absolutes (do not violate)

- **Orca stays a frontend.** No reimplementation of RHO/PRM/GRPO/gradient
  extraction in Orca. Those remain bash in mini-ork, triggered by an Orca
  automation that calls `mini-ork-reflect`. Orca only ever issues `SELECT`s
  against the learning DB and `GET`s against CN.
- **Learning DB is opened READ-ONLY** from Orca (`sync-database.ts` with
  `readonly: true`). Orca must never `UPDATE`/`INSERT` into the learning tables
  except the single sanctioned trace-write in the agent-hooks path (see Scope 3),
  and even that should prefer appending via the existing mini-ork trace writer if
  reachable rather than raw SQL.
- **CN canonical write path is event ingest only.** Orca POSTs
  `/api/v1/cc/hook/{event}` and `/api/v1/agent/outcome` — it MUST NOT write
  memories via any `tools/store` endpoint. This preserves CN's single-entry WAL
  pipeline. Mirror the rule in `lib/cn_client.sh:25-27`.
- **Kill-switch parity.** Honor `MO_DISABLE_CN=1` (silent no-op for all CN
  calls) and degrade gracefully when CN is unreachable (cache reachability,
  short timeouts: 8s read / 3s fire-and-forget).
- **Strip `<z-insight>…</z-insight>`** from any agent transcript before parsing
  JSON or extracting consumed atom ids (`cn-[a-f0-9]{8,40}`). Mini-ork does this
  in `lib/llm-dispatch.sh:_mo_llm_strip_protocol_blocks`; port the regex
  (DOTALL, also handle an unterminated trailing block).
- Match Orca's existing conventions: `node:sqlite` via
  `src/main/sqlite/sync-database.ts`, RPC-method modules under
  `src/main/runtime/rpc/methods/`, CLI specs under `src/cli/specs/`, contextBridge
  in `src/preload/index.ts`, Redux slices in the renderer. Mirror
  `orchestration.ts` as the template for the new `mini-ork` RPC surface.

## Scope (in scope)

1. **Backend adapters in Orca main (`src/main/`).**
   - `src/main/contextnest/client.ts` — TypeScript port of the **read side** of
     `lib/cn_client.sh`: `health`, `retrieve(query,limit)`,
     `capsule(query,since,project)`, `features(since,layer)`,
     `inbox(limit,urgency)`, `sessionsByFile/Feature/Intent`,
     `basins(project,limit)`, `connections(nodeId,limit)`. Same base URL,
     timeouts, reachability cache, `MO_DISABLE_CN` honoring. Plus the **write
     side**: `postHook(event,session_id,cwd,transcript_path)` and
     `postOutcome(atom_ids,outcome,evidence,session_id)` (fire-and-forget).
   - `src/main/learning/db.ts` — read-only `sync-database.ts` opener over
     mini-ork's learning SQLite file (path from config/env, e.g.
     `MINI_ORK_DB`). Typed query helpers: `tracesForRun(run_id)`,
     `gradientsForRun(run_id)` (join `gradient_records.evidence = trace_id`),
     `winRates(task_class?)`, `laneAdvantage(task_class?)`, `ideaTree(root_id)`,
     `runList(limit)`.
   - `src/main/runtime/rpc/methods/mini-ork.ts` — RPC methods mirroring
     `orchestration.ts`: `mini-ork.runList`, `mini-ork.runTrajectory(run_id)`
     (the stitched object), `mini-ork.kickoff(recipe,kickoffPath)` (spawns
     `bin/mini-ork run` / emits plan — see Scope 4), `learning.gradients`,
     `learning.winRates`, `learning.laneAdvantage`, `learning.ideaTree`,
     `contextnest.capsule|retrieve|inbox|basins`. Register in the RPC index;
     extend the mobile RPC allowlist with the **read-only** subset.
   - `src/cli/specs/mini-ork.ts` + handler — `orca mini-ork run-list|trajectory|
     kickoff|gradients|capsule` so agents/scripts can drive it headless.

2. **Renderer panels (`src/renderer/src/`).**
   - Redux slice(s) + IPC client hooks for `learning:*` and `contextnest:*`.
   - **Memory panel** — CN capsule (kind-ordered: risks→decisions→failures→…),
     atoms with similarity, inbox (urgency filter), basins.
   - **Learning panel** — gradients (confidence-ranked, each linking to its
     source trace), prompt win-rates, lane relative-advantage (which model wins
     per `task_class`), idea-tree (parent/child from `idea_tree_nodes` —
     already a tree, render as collapsible).
   - **Run Trajectory view** — the unified centerpiece stitched by `run_id`
     (regions ①–⑤ from the design). Each DAG node deep-links to its Orca
     worktree/terminal + diff.

3. **agent-hooks write path (`src/main/agent-hooks/server.ts`).**
   On agent-stop for an Orca-managed agent that ran a mini-ork node:
   - strip `<z-insight>` from the transcript;
   - write/append one `execution_trace` (prefer mini-ork's `trace_write` if
     invokable; else sanctioned append);
   - `contextnest.postHook('subagent_stop', …)` (transcript ingest);
   - extract consumed `cn-…` atom ids from the injected prompt/prefetch and
     `contextnest.postOutcome(atom_ids, success|failure|neutral, evidence,
     session_id)`. This replaces mini-ork's entire `hooks/` dir for
     Orca-launched runs.

4. **Dispatch wiring — recipe DAG → Orca worktrees.**
   - `bin/mini-ork-plan` emits a `plan.json` DAG (nodes, edges, dispatch mode,
     lane→agent, per-node context-pack/prompt). Define and document the schema
     in the kickoff's companion (see Deliverable docs).
   - Orca's `OrchestrationDb` ingests `plan.json`: nodes → `tasks` rows, edges →
     `parent_id`/deps, dispatch mode → `dispatch` messages, reviewer
     REJECT/ESCALATE → `decision_gate`/`escalation`.
   - Each node → `orca-runtime.ts:createManagedWorktree` with the lane's agent
     (`createdWithAgent`) and a `startupCommand` whose prompt is **prepended
     with the CN capsule + high-confidence gradients** (replaces
     `context_assembler.sh` / `subagent-prefetch.sh` injection).

5. **Prompt injection at worktree creation.** Single call site in
   `createManagedWorktree`: build `capsule + role-pack + gradients(conf≥0.6)` and
   prepend to the startup prompt. Config flags `MO_USE_ROLE_PACKS`,
   `MO_INJECT_LEARNINGS` parity.

## Out of scope

- No reimplementation of the learning aggregators (RHO/PRM/GRPO/gradient
  extraction) in TypeScript — they stay in mini-ork bash.
- No new CN write endpoints; no `tools/store` writes from Orca.
- No changes to mini-ork's learning schema or recipe engine beyond emitting
  `plan.json` from `mini-ork-plan`.
- No mobile-specific UI beyond exposing the read-only RPC subset through the
  allowlist (full mobile panels are a follow-up).
- No auth/multi-user/remote-CN concerns (assume local CN on `:28080`).

## Definition of Done

- `contextnest/client.ts` + `learning/db.ts` + `rpc/methods/mini-ork.ts` +
  `cli/specs/mini-ork.ts` land, registered, typed; learning DB opened read-only;
  CN client honors `MO_DISABLE_CN`, timeouts, reachability cache, and the
  event-only write rule.
- Memory panel, Learning panel, and the unified Run Trajectory view render real
  data from a live mini-ork run + a reachable CN, and degrade cleanly when CN is
  down or `MO_DISABLE_CN=1`.
- `agent-hooks/server.ts` records a trace + POSTs CN hook + outcome for an
  Orca-launched mini-ork node; transcripts have `<z-insight>` stripped before
  parsing; consumed atom ids are extracted and reported.
- `bin/mini-ork-plan` emits a documented `plan.json`; Orca ingests it into
  `OrchestrationDb` and a real recipe runs as N worktrees with diff review;
  reviewer verdicts surface as decision gates.
- End-to-end smoke: kick off a recipe from Orca → see nodes as worktrees →
  traces + gradients appear in the Learning panel → outcome posted to CN
  (confidence delta visible) → the whole loop visible in one Run Trajectory view.
- Tests: unit tests for `contextnest/client.ts` (mock REST) and `learning/db.ts`
  (`:memory:` fixtures), plus the z-insight stripping regex; mirror Orca's
  existing `*.test.ts` placement.

## Validation gate (epic-sized — required)

Per repo policy, before claiming done: panel review across lenses +
Krippendorff-α + `citation_verifier_mechanical` + refute-or-promote. Single-lens
self-review is insufficient. Code nodes use minimax/codex lanes (never glm; glm
analysis-only).

## Companion docs to produce during the run

- `plan.json` schema spec (the Orca↔coordinator handoff contract).
- `ContextNestClient.ts` interface table mapped 1:1 to `lib/cn_client.sh`
  endpoints (method → verb → path → request → response shape).
- Learning-DB query surface (the read-only `SELECT`s Orca issues).
