# mini-ork Prompt Catalog

43 prompts + this file + TEMPLATING.md = 45 total.

All prompts use `{{PLACEHOLDER}}` syntax for project-specific values. See
`TEMPLATING.md` for the full placeholder reference and how to add your own.

---

## Stage: Decomposer

| File | Description |
|------|-------------|
| `decomposer.md` | Splits a monolithic epic into N≤7 independent sub-epics with BDD roles, feature_kind tags, and DoD probes |

---

## Stage: Worker

| File | Description |
|------|-------------|
| `worker-task.md` | *(not present — use project-specific worker prompt)* |
| `spec-author.md` | Writes a full BDD spec (feature file + Playwright scaffolding) from a kickoff |
| `spec-author-step-a.md` | Step A of spec authoring: scope analysis + scenario draft before committing to full spec |

---

## Stage: Reviewer

| File | Description |
|------|-------------|
| `spec-reviewer.md` | Reviews a worker's BDD spec for completeness, coverage, and harness compliance |
| `reviewer-verdict.md` | *(not present — logic is embedded in spec-reviewer.md)* |
| `rubric-prescreen.md` | Cheap pre-flight checklist (8 rubric items) before running expensive Playwright BDD |
| `mutation-adversary.md` | Adversarial reviewer that deliberately probes edge-cases the worker might have missed |

---

## Stage: Self-Heal / Feedback

| File | Description |
|------|-------------|
| `reflection-refiner.md` | Converts BDD failure logs into root-cause hypotheses for the next worker iteration |
| `self-correction.md` | Applies surgical fixes to reviewer feedback — smallest possible patch |
| `self-correction-patch.md` | Patch-only (unified diff) mode of self-correction; ~80% token reduction on single-issue fixes |
| `feedback-iterate.md` | *(not present — inline in orchestrator loop)* |

---

## Stage: Bug-Hunt

| File | Description |
|------|-------------|
| `bug-hunter.md` | Parameterized base hunter — supports any role (correctness, security, ux_a11y, perf) |
| `bug-hunter-corr.md` | H-CORR specialist: logic correctness + state-machine integrity |
| `bug-hunter-cron.md` | H-CRON specialist: cron sweeps, stuck-job health, sandbox lifecycle |
| `bug-hunter-data.md` | H-DATA specialist: schema, migrations, JSONB invariants, column drift |
| `bug-hunter-sec.md` | H-SEC specialist: auth bypass, IDOR, injection, SSRF, PII leakage |
| `bug-hunter-wire.md` | H-WIRE specialist: boot wiring, env var coupling, feature-flag misconfig |
| `bug-hunter-tier3.md` | Tier-3 (lower-priority) bug hunter for non-critical surfaces |
| `bug-fixer.md` | Opus dedupe-validate-fix agent; consumes hunter NDJSON, emits round report |

---

## Stage: Perf-Hunt

| File | Description |
|------|-------------|
| `perf-hunter-be.md` | BE performance hunter: Loki/Tempo trace analysis, slow routes, N+1 queries |
| `perf-hunter-fe.md` | FE performance hunter: Lighthouse, bundle analysis, render path |
| `perf-hunter-db.md` | DB performance hunter: pg_stat_statements, EXPLAIN ANALYZE, index gaps |
| `perf-fixer.md` | Perf fix agent: applies minimal patch from perf-hunter verdict |

---

## Stage: Refactor Stage 1 — ARCH-SPEC Hunters

| File | Description |
|------|-------------|
| `refactor-arch-struct.md` | A1-STRUCT: finds scattered authority, wrong layer ownership, missing abstractions |
| `refactor-arch-behav.md` | A1-BEHAV: finds state-machine fragmentation, async boundary mismatches, silent failure paths |
| `refactor-arch-env.md` | A1-ENV: finds env-var coupling, hard-coded endpoints, untyped side effects |
| `refactor-arch-consensus.md` | Stage 1 ConsensusGate: dedup + rank + filter hunters' ARCH-SPEC candidates |
| `refactor-adr-writer.md` | Writes a durable ADR from a shipped ARCH-SPEC |

---

## Stage: Refactor Stage 2 — MODULE-PLAN

| File | Description |
|------|-------------|
| `refactor-module-bound.md` | A2-BOUND: proposes 3-5 Pareto-front module boundary candidates (seam drawing) |
| `refactor-module-deps.md` | A2-DEPS: validates dependency-closure for each proposed boundary |
| `refactor-module-name.md` | A2-NAME: proposes file + symbol names for new modules, flags collisions |
| `refactor-module-consensus.md` | Stage 2 ConsensusGate: merges BOUND+DEPS+NAME into Pareto-front MODULE-PLAN |

---

## Stage: Refactor Stage 3 — ATOM-PRS

| File | Description |
|------|-------------|
| `refactor-atom-decompose.md` | Decomposes MODULE-PLAN into individually-shippable atomic PRs with DAG |
| `refactor-atom-validator.md` | Stage 3 ConsensusGate: validates DAG acyclicity, frame consistency, test gates |

---

## Stage: Refactor Stage 4 — DSAP Annotators

| File | Description |
|------|-------------|
| `refactor-annotate-component.md` | A1-COMPONENT: structural lens — task, callers, callees, inputs/outputs |
| `refactor-annotate-behavior.md` | A1-BEHAVIOR: Hoare-triple lens — pre/post conditions, guards |
| `refactor-annotate-environment.md` | A1-ENVIRONMENT: side-effect lens — mutating flag, side_effects list, frame |
| `refactor-annotate-consensus.md` | Layer 1 ConsensusGate: merges 3 lens annotations per function |
| `refactor-fix.md` | Layer 3 Fix agent: proposes minimal patch from validation verdict |
| `refactor-validator.md` | Layer 3 Validator: verifies Hoare triples against live code + Loki signals |

---

## Stage: Refactor Suggestions

| File | Description |
|------|-------------|
| `refactor-suggest-dup.md` | Identifies duplication candidates for consolidation |
| `refactor-suggest-layer.md` | Identifies layer-violation candidates (wrong abstraction level) |
| `refactor-suggest-name.md` | Suggests naming improvements for symbols and files |
| `refactor-suggest-name_b.md` | Variant B of name suggester (broader scan, lower precision) |

---

## Top 5 Showcase Prompts

These are the prompts most worth reading first if you want to understand mini-ork's design:

1. **`decomposer.md`** — the entry-point for all epic work; its `bdd_role` + `feature_kind` tags drive every downstream stage
2. **`bug-fixer.md`** — best example of a multi-step agent with citation-gate (A5), vote-mode awareness, and structured round-report output
3. **`spec-author.md`** — shows the BDD-first pattern: scenario-first, then implementation
4. **`refactor-arch-consensus.md`** — the ConsensusGate pattern: dedup + rank + filter across multiple hunter outputs
5. **`reflection-refiner.md`** — grounded in TENET paper; shows how to convert raw test failure into actionable root-cause feedback in ≤300 words
