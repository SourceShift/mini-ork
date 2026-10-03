# spec-driven-delivery

`spec-driven-delivery` is an outer recipe that delivers every feature
described by a directory of markdown spec files. The spec is the sole source
of truth. Each spec is compiled into a SpecCard contract
(`schemas/spec-card.schema.json`). Executable acceptance gates are derived
before any implementation, on a lane separate from the implementer. Each
atomic deliverable then goes through its own `recursive-validate-impl` child
run, and the result is verified against a live surface.

Use it when the input is a spec directory (`## Spec dir:` in the kickoff)
and every spec should ship with executable gates. For a single broad
document, use `doc-to-features-loop`. For a single clear task, use
`recursive-validate-impl` directly.

Design: `docs/plans/2026-10-03-spec-driven-delivery.md`.

## DAG

Mirrors `workflow.yaml`. Every verifier's fail edge escalates to `reflector`.

```
specdir_ingest       (verifier)                 spec-index.json via `mini-ork specs ingest`
  -> contract_compiler   (researcher, lane planner)   spec-cards/<spec_id>.json + ratification/<spec_id>.json
  -> ratification_check  (verifier)             every source requirement covered or acknowledged
  -> spec_lint           (verifier)             authoring-error lint
  -> test_author         (researcher, lane reviewer)  gates/<spec_id>.json, written before implementation
  -> test_validity       (verifier)             broken-baseline: probes fail on the untouched tree
  -> per_spec_dispatcher (implementer, lane implementer)  one recursive-validate-impl run per deliverable
  -> dispatch_aggregator (verifier)             aggregate-verdict.json recomputed from child runs
  -> smoke_live          (verifier)             every acceptance gate against the live surface
  -> ui_craft_gate       (verifier)             pluggable judge via MO_SDD_UI_GATE_CMD
  -> ledger_writer       (verifier)             ledger.jsonl + slide-back gauges
  -> publisher

reflector -> replanner -> contract_compiler   (recursive; max 6 iterations, $100 total)
```

`contract_compiler` and `test_author` are typed `researcher` so their prompts
run as artifact-producing tasks. A `reviewer` node would be turned into a
panel-verdict synthesizer, which would hand an LLM verdict authority.
Authorship separation is declared in `task_class.yaml`
(`heterogeneity.authorship_separation`). Contracts and gates come from the
`planner` and `reviewer` lanes; code comes from `implementer` and the child
recipe's `worker` lane.

Run artifacts in `${MINI_ORK_RUN_DIR}`: `spec-index.json`,
`spec-cards/<spec_id>.json`, `ratification/<spec_id>.json`,
`gates/<spec_id>.json`, `dispatch-results.json`, `asks/<spec_id>--<deliverable_id>.json`,
`aggregate-verdict.json`, `smoke-live.json`, `ledger.jsonl`,
`slide-back.json`, `reflector.json`, `replan.json`.

## Environment

| Variable | Default | Effect |
|---|---|---|
| `MO_SDD_SPEC_GLOB` | `*.md` | Which files in the spec dir are specs. |
| `MO_SDD_MIN_SPECS` | `1` | Ingest fails below this many specs. |
| `MO_SDD_UI_GATE_CMD` | empty | UI-craft judge command template. When empty, the UI-craft gate is skipped and a WAIVED ledger row is written; the slide-back gauge counts it. |
| `MO_SDD_SMOKE_TIMEOUT_S` | `120` | Per-probe timeout for test-validity and smoke. |
| `MO_SDD_SURFACE_ENV` | empty | Dotenv file passed to smoke probes (base URLs, tokens). |
| `MO_SDD_MAX_PARALLEL_SPECS` | `1` | Specs dispatched at once; serial by default, like the scheduler. |

## Rules

1. **Spec is the only mutable artifact.** Implementer lanes never edit specs
   or SpecCards. "Spec is wrong or unsatisfiable" routes to a contract
   revision: a structured ASK pauses the deliverable instead of guessing.
2. **Authorship separation.** The lane that writes contracts and tests is
   never the lane that implements against them. The verifier payload is the
   only verdict; rubric scores and self-reports are advisory.
3. **Test-first.** Gates exist and pass `test_validity` before the first
   implementer cycle spends budget. Broken baseline: a gate that already
   passes on the untouched tree is vacuous and is rejected.
4. **Gates execute behavior.** Smoke runs against a live surface; a green
   compile or lint is never a verdict. Each gate passes or fails atomically
   with a reason; there is no partial credit.
5. **Regenerate from spec.** A failed cycle restarts from the SpecCard plus
   the latest gate evidence, never from chat history or a patch of a patch.
6. **Traceability.** Ledger rows link clause, deliverable, run_id, commit,
   and gate verdicts. Slide-back gauges track the waived-gate count and the
   spec-edit vs code-edit ratio. A human veto is a logged ledger event.
7. **One deliverable = one inner kickoff** (planner-truncation guard).

## Status

Scaffold (K2). All eight `verifiers/*.py` are stubs that print
`{"pass": false, "reason": "NOT_IMPLEMENTED"}` and exit 2 until K3
(`kickoffs/sdd/k3-verifiers.md`). A live run fails closed at
`specdir_ingest`; do not launch the recipe before K3 lands.
