# spec-driven-delivery — SDD mode for mini-ork

Status: DESIGN (authoritative for the `feat/spec-driven-delivery` build)
Informed by: "Spec-Driven Development: From Specs to Code with AI Agents"
(10 chapters; arXiv 2505.08903, 2506.23762, 2508.15503, 2512.21347,
2601.11655, 2602.07079, 2602.23047, 2604.26192, ACM 10.1145/3695988 et al.)

## Problem

mini-ork can turn ONE markdown doc into a feature queue (`doc-to-features-loop`)
and close-loop a single feature (`recursive-validate-impl`), but it has no mode
that takes a DIRECTORY of .md spec files and delivers every feature they
describe with (a) the spec as the sole source of truth, (b) executable
acceptance gates derived BEFORE implementation, (c) live smoke verification
against a running surface, and (d) a UI/UX craft gate. Field experience shows
why each is mandatory: campaign smoke specs have shipped vacuous or unpassable
(20/32 in one audited wave), rubric self-reports have been mistaken for
verdicts, and function-green/craft-dead UI has shipped.

## Deliverable overview

1. **`mini_ork/specdir/`** — deterministic ingestion library + `mini-ork specs`
   CLI (no LLM): scan a spec dir, build `spec-index.json`, lint specs.
2. **`recipes/spec-driven-delivery/`** — outer recipe: compile each spec into a
   SpecCard contract, derive executable gates test-first, dispatch each atomic
   deliverable through `recursive-validate-impl`, verify with live smoke +
   optional UI-craft gate, write a traceability ledger, reflect/replan.
3. **Verifiers** — deterministic Python: spec lint, contract ratification,
   test-validity (broken-baseline), smoke-live runner, ledger/slide-back gauges.
4. **Fixture + e2e dry-run eval + docs.**

## SpecCard (the compiled contract)

Schema at `schemas/spec-card.schema.json`. One SpecCard per spec file:

```yaml
spec_id: s11-owner-dashboard          # slug, unique in index
source_path: /abs/path/session-11.md  # the .md is the source of truth
source_hash: sha256:…                 # contract is void if the source drifts
title: …
clauses:                              # SGRM four-component split
  functional: [ {id: F1, text: …} ]
  quality:    [ {id: Q1, text: …} ]
  constitutional: [ {id: C1, text: …} ]   # hard rules / never-do
  architectural:  [ {id: A1, text: …} ]
acceptance:                           # every criterion → ONE executable gate
  - id: AC1
    clause_refs: [F1]
    text: …
    gate:
      kind: cmd | ui | contract       # cmd: shell probe; ui: UI probe; contract: assertion
      probe: "<command or probe template>"
      expect: "<pass condition>"
deliverables:                         # atomic, dependency-ordered, 1 kickoff each
  - id: D1
    title: …
    acceptance_refs: [AC1, AC2]
    depends_on: []
ui_craft: { required: true|false, design_sources: [paths] }
status: draft | ratified | dispatched | delivered | failed | blocked
```

## Pipeline (workflow DAG)

```
specdir_ingest (deterministic)             spec-index.json
  → contract_compiler (LLM lane A)         SpecCard per spec
  → ratification_check (verifier)          every source requirement covered or flagged
  → spec_lint (verifier)                   7 authoring errors, 4-property test
  → test_author (LLM lane B ≠ implementer) per-acceptance probes + smoke spec
  → test_validity (verifier)               broken-baseline: probes FAIL on untouched tree;
                                           non-vacuous; spec-coverage
  → per_spec_dispatcher                    inner recursive-validate-impl per deliverable,
                                           SpecCard as sole anchor
  → dispatch_aggregator (verifier)
  → smoke_live (verifier)                  run every acceptance gate against live surface
  → ui_craft_gate (verifier, conditional)  pluggable judge via MO_SDD_UI_GATE_CMD
  → ledger_writer (verifier)               spec-clause → run → commit → verdict
  → reflector → replanner → (recursion)    divergence_kill as in sibling recipes
  → publisher / rollback
```

## Non-negotiable rules (from the book, encoded as mechanism)

1. **Spec is the only mutable artifact.** Implementer lanes never edit specs or
   SpecCards; "spec is wrong/unsatisfiable" routes to a contract-revision step
   (structured ASK artifact pauses the deliverable, never guess).
2. **Authorship separation.** The lane that writes contracts/tests is never the
   lane that implements against them. The verifier payload is the only verdict;
   rubric/self-report is advisory.
3. **Test-first.** Gates exist and pass `test_validity` BEFORE the first
   implementer cycle spends budget. Broken-baseline: a gate that passes on the
   untouched tree is vacuous → reject.
4. **Gates execute behavior.** Live smoke against a running surface; compile/
   lint green is never a verdict. Atomic pass/fail with reason; no partial credit.
5. **Regenerate from spec.** A failed cycle restarts from SpecCard + latest gate
   evidence, never from chat history or patch-the-patch.
6. **Traceability.** Ledger rows: clause → deliverable → run_id → commit →
   gate verdicts. Slide-back gauges: waived-gate count, spec-edit vs code-edit
   ratio. Human veto point is a logged ledger event.
7. **One deliverable = one inner kickoff** (planner-truncation guard).

## Env surface (new)

- `MO_SDD_SPEC_GLOB` (default `*.md`), `MO_SDD_MIN_SPECS` (default 1)
- `MO_SDD_UI_GATE_CMD` — judge command template; empty ⇒ ui_craft skipped with
  a ledger WAIVED row (visible, counted by slide-back gauge)
- `MO_SDD_SMOKE_TIMEOUT_S` (default 120), `MO_SDD_SURFACE_ENV` — dotenv passed
  to smoke probes (base URLs, tokens)
- `MO_SDD_MAX_PARALLEL_SPECS` (default 1 — serial like scheduler)

## Rollout

Build on `feat/spec-driven-delivery` via mini-ork's own closed loop
(`recursive-validate-impl`, framework-cwd allowed). Merge upstream main after
e2e fixture dry-run passes. Consumers re-vendor as usual.
