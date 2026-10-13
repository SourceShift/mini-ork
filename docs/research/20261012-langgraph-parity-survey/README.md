# LangGraph parity survey — synthesis README (D2)

**Snapshot date:** 2026-10-12
**Survey family:** `20261012-langgraph-parity-survey`
**Scope:** consolidating entry point for the three D1 lens reports (LangGraph
auditor, orchestration-space surveyor, gap-finder) under the chapter-write
consolidation epic. This file does not introduce new claims — every ranked
top-10 row, every chapter-epic blocker, and every positioning ingredient is
drawn 1:1 from a sibling sidecar's structured payload, so the synthesis is
machine-checkable against the sidecars.

## Positioning statement (F2)

mini-ork is the best **zero-dependency, sandbox-native LangGraph alternative
for any task**: it ships the same **core semantics** as the floor — typed
graph, state channels, nodes, edges, conditional edges, the `Command`
analog (`outputs:` + `retries`-with-`condition:`), retry policy, structured
output, MCP, run config — and the **isolation**, **portability**, and
**adaptivity** primitives as the edge — single-writer lease
(`mini_ork/stores/lease.py`), sandbox-friendly execution, the recipe-driven
model, the operator-steering channel, the recursion block, per-recipe
(not per-graph) deployment via `mini-ork serve`. The floor is parity; the
edge is why the floor is worth shipping on mini-ork and not elsewhere.

- **Floor — core semantics (must-have parity):** graph, state, nodes, edges,
  conditional edges (jsonschema-backed `condition:`), `Command` analog,
  retry policy, structured output, MCP, run config.
- **Edge — isolation, portability, adaptivity (differentiation):** single-writer
  lease, sandbox-native execution, recipe-driven per-deployment packaging,
  operator-steering channel, recursion block, per-recipe deployment.

## How to read this README

- §"Positioning statement (F2)" — the F2 floor/edge split, derived from the
  auditor's "Coverage map" (breadth-defensible baseline) and the
  orchestration-space surveyor's "Cross-cutting reads" (anchor surfaces and
  partial-surface cluster).
- §"Merged top-10 backlog (F3)" — the cross-lens, deduplicated top-10; one
  line per entry with `(rank, feature, source, phase, effort)`, drawn from
  the gap-finder sidecar's `top_10_backlog[]` array.
- §"Chapter-epic remaining blockers (F5)" — the F5 list of blockers
  UNRESOLVED by P1, ranked by chapter-epic criticality, drawn from the
  gap-finder sidecar's `chapter_epic_remaining_blockers[]` array.
- §"Per-lens pointers" — the three lens reports and their sidecar JSONs,
  with the per-lens scope note from each report's preamble.

## Merged top-10 backlog (F3)

The cross-lens, deduplicated top-10 in rank order. One line per entry:
`(rank, feature, source, phase, effort)`. The gap-finder sidecar's
`top_10_backlog[]` is the canonical merge — every row maps 1:1 to a row in
that array, and every row is independent of the others on the five fields.

1. **Thread / checkpoint id** — *attempt_sha on `RunContext` + `<output>.attempt-<sha>.json` sidecar* — source: langgraph — phase: P2 — effort: medium
2. **Durable execution** — *`<run_dir>/.receipts/<tool>.json` per-tool receipts table* — source: langgraph — phase: P2 — effort: high
3. **Human-in-the-loop** — *consolidate the §14 / §15 decision on one surface* — source: langgraph — phase: P2 — effort: high
4. **Interrupts / breakpoints** — *dynamic in-process `interrupt(...)` callable + `Command(resume=...)`* — source: langgraph — phase: P2 — effort: high
5. **Persistence chapter epic** — *`MO_PERSISTENCE_BACKEND` + `MO_CHECKPOINT_FORK`* — source: langgraph — phase: P2 — effort: high
6. **Streaming** — *`StreamSink` class + `Stream` envelope + per-channel subscribe CLI* — source: langgraph — phase: P2 — effort: high
7. **Sub-recipe node** — *`type: subrecipe` + `recipe_ref` on `WorkflowNode`* — source: langgraph — phase: P2 — effort: high
8. **Time travel** — *`mini-ork fork --run --node --sha` + attempt-<sha> sidecars* — source: langgraph — phase: P2 — effort: high
9. **Apache Airflow pools + SLAs** — *named-pool map + per-task SLA field* — source: apache_airflow — phase: P2 — effort: medium
10. **Asset-keyed parity (catalog slice)** — *per-asset catalog table behind `run_summary`* — source: prefect — phase: P2 — effort: medium

**Format note (F3 invariant).** Every entry above carries all five fields
(`rank`, `feature`, `source`, `phase`, `effort`); no feature string is
duplicated across the ten rows; the ten features together cover one
persistence-side claim, one durability-claim, one HITL claim, one
streaming claim, one composition claim, one rewind claim, one
operational-tooling claim, and one asset-keyed claim — so the merged
backlog is breadth-defensible, not a single-axis list.

## Chapter-epic remaining blockers (F5)

The chapter-write consolidation epic is the load-bearing story this survey
frames. The blockers below are the F5 list — every entry is UNRESOLVED by
P1 per F5's hard rule, and ranked by chapter-epic criticality. Sources:
the gap-finder sidecar's `chapter_epic_remaining_blockers[]` array, in
array order (B1 → B10).

- **B1 — Persistence chapter epic** — three coupled decisions not yet made.
  `MO_PERSISTENCE_BACKEND` + `MO_CHECKPOINT_FORK` + `attempt_sha` +
  `<output>.attempt-<sha>.json` sidecar (langgraph §8 / §9 / §24) are all
  parallel; resolving one without the others leaves the surface
  incoherent. The chapter epic should ship them as one chapter slice or
  document the dependency explicitly. Source lens: langgraph.
  Phase: P2.
- **B2 — Operational tooling** — scheduler + per-pool concurrency + SLAs.
  mini-ork has no real scheduler wired into the engine entry, no per-pool
  concurrency (the single `_max_parallel` at
  `mini_ork/cli/execute.py:398` is global), and no per-task SLAs. Airflow
  §3.2 + §3.4 and Temporal §1.5 are all blocked on this. Source lens:
  space. Phase: P2.
- **B3 — Durable execution (journal-driven replay + continue-as-new)** —
  `run_events` is the journal; `classify_outcome` at
  `mini_ork/orchestration/runner_orchestrator.py:216` reads only the last
  row today. Read the whole history on resume and add an in-run
  continue-as-new analogue to bound history growth. Source lens: space.
  Phase: P2.
- **B4 — Streaming chapter epic** — `StreamSink` class or LiveWriter-only.
  The single largest surface divergence after persistence; the chapter
  epic owns deciding whether to ship `mini_ork/dispatch/stream.py` with
  a `StreamSink` class + `Stream` envelope +
  `mini-ork subscribe --run --channel --sink` CLI, or keep `LiveWriter`
  at `mini_ork/dispatch/live_stream.py:80` as the only declared streaming
  primitive. Source lens: langgraph. Phase: P2.
- **B5 — Interrupt / HITL chapter epic** — single surface or two. Decide
  between `type: human_in_the_loop` + `finish_reason: "interrupt"` +
  `<run_dir>/.steering/<node>.json` payload, or keep the
  `human_decision_gate` edge + operator-steering channel (see
  `docs/OPERATOR-STEERING.md` + `mini_ork/steering/mid_node_injector.py`)
  as the documented answer. Source lens: langgraph. Phase: P3.
- **B6 — Asset-keyed parity** — asset-keyed runs + sensors + catalog +
  lineage. Promote `epic_id` to a first-class asset key (today opaque to
  `mini_ork/ide_pages/dags.py:97`), add a `wait_for_state` node type,
  surface the catalog table behind `run_summary`, and add a lineage view
  alongside `_classification_dag`. Source lens: space. Phase: P3.
- **B7 — Saga / compensating handlers** — rollback path for fan-out to
  side effects. `merge_decisions` at
  `mini_ork/orchestration/recursive.py:436` covers forward merge only;
  there is no saga-rollback path. Source lens: space. Phase: P3.
- **B8 — Resume tokens** — checkpoint-token suspend that survives long
  approvals. `pause_cost_run` at `mini_ork/web/control.py:46` is
  cost-only today; Argo §2.4 + Windmill §6.5 share the suspend-with-token
  gap. Source lens: space. Phase: P3.
- **B9 — Secrets (vault tier with env-tier fallback)** — adopt the vault
  surface described in `config/vault.example.yaml` so per-environment
  secrets stop living as YAML literals. The env tier (today's contract
  at `mini_ork/context.py:94`) keeps the simple-tier shape; the vault
  tier is the opt-in for shared / hostile deployments. Source lens:
  space. Phase: P3.
- **B10 — Four-lens chapter synthesis pending** — the public-facing
  chapter synthesis document is not yet authored. The D1 auditor +
  D1 gap-finder pair sit alongside D2 (stategraph construction), D3
  (persistence / checkpointers), and D4 (streaming modes); this README
  is the consolidating D2 sibling, not the public chapter synthesis
  itself. Source lens: langgraph. Phase: P3.

## Per-lens pointers

The three lens reports and their structured sidecar JSONs live alongside
this README in the same directory.

- **LangGraph auditor** — [report-langgraph-auditor.md](report-langgraph-auditor.md)
  covers 24 concepts across 13 areas (graph construction, state, routing,
  persistence, memory, parallelism, HITL, streaming, tools, structured
  output, resilience, runtime, deployment, external protocols). Every
  concept has either a YAML-snippet equivalent or a `divergent-by-design:`
  line with a one-line rationale. Sidecar:
  [context-langgraph-auditor.json](context-langgraph-auditor.json).
- **Orchestration-space surveyor** — [report-space-surveyor.md](report-space-surveyor.md)
  surveys seven external systems — Temporal, Argo Workflows, Apache
  Airflow, Prefect / Dagster (paired), Restate, Windmill, and Ray — and
  grades each candidate feature against an existing partial surface in
  the engine or calls it out as a gap, with a `file:line` citation that
  was re-grepped at authoring time. Sidecar:
  [context-space-surveyor.json](context-space-surveyor.json).
- **Gap-finder** — [report-gap-finder.md](report-gap-finder.md) is the
  cross-lens synthesis; its 8-column F4 backlog and F5 blocker list are
  the inputs the merged top-10 and chapter-epic blockers sections above
  cite 1:1. Sidecar:
  [context-gap-finder.json](context-gap-finder.json).

## Provenance

- **Merged top-10 (F3)** — the gap-finder sidecar's `top_10_backlog[]`
  array, ordered by `rank`. Each row in §"Merged top-10 backlog (F3)"
  maps 1:1 to a row in that array, with the `feature` field taken
  verbatim from `title` and the `source` field taken from the
  corresponding `claims[]` entry's `source_system` (so the synthesis
  preserves the original source attribution rather than re-attributing
  the merged row to the gap-finder).
- **Chapter-epic blockers (F5)** — the gap-finder sidecar's
  `chapter_epic_remaining_blockers[]` array, in array order (B1 → B10).
  Each blocker's `source_lens` and `F5_phase_status` fields are quoted
  directly; the body text is condensed from the sidecar's `detail` field
  without introducing new claims.
- **Positioning statement (F2)** — derived from the auditor's "Coverage
  map" (the breadth-defensible baseline across 13 areas) and the
  orchestration-space surveyor's "Cross-cutting reads" (the three
  partial-surface primitives — `run_events` journal, `input_hash` cache,
  steering channel). The F2 floor/edge split is the explicit split the
  positioning statement above names.
- **Citation convention (C4)** — every claim in every lens report
  carries an inline citation on the same line; the sidecars preserve
  this invariant via the `citations: []` field on every claim, and the
  F4 8-column rows in the gap-finder sidecar keep a `Source claim ref`
  cell on every row. This README does not re-cite at the row level —
  it points at the lens reports and sidecars above for the per-row
  citation trail.

## Seven-file set

The seven files this survey family ships, all in this directory
(`docs/research/20261012-langgraph-parity-survey/`):

- `README.md` — this file (the D2 consolidating entry point).
- `report-langgraph-auditor.md` — LangGraph v1.x auditor's 24-concept
  parity report.
- `context-langgraph-auditor.json` — auditor's machine-readable sidecar.
- `report-space-surveyor.md` — orchestration-space surveyor's
  7-system feature report.
- `context-space-surveyor.json` — space surveyor's machine-readable
  sidecar.
- `report-gap-finder.md` — cross-lens gap-finder's F4 / F5 report.
- `context-gap-finder.json` — gap-finder's machine-readable sidecar.
