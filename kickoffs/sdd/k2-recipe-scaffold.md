# K2 — `recipes/spec-driven-delivery/` scaffold (yaml trio + prompts)

Design doc (authoritative, read first):
`/Users/admin/ps/mini-ork-sdd-wt/docs/plans/2026-10-03-spec-driven-delivery.md`
Prerequisite (already on this branch): `mini_ork/specdir/` from K1.

## Goal

Create the `spec-driven-delivery` recipe directory: `task_class.yaml`,
`artifact_contract.yaml`, `workflow.yaml` implementing the design doc's DAG,
all LLM prompts, `README.md`, and `example-kickoff.md`. Verifier scripts are
REFERENCED by path but implemented in the next kickoff — create each as a
stub that exits 2 with `{"pass": false, "reason": "NOT_IMPLEMENTED"}` on
stdout so the DAG is structurally complete and schema-valid now.

## Feature scope

- `recipes/spec-driven-delivery/task_class.yaml` — task_class
  `spec_driven_delivery`, version 0.1.0, keyword/regex matches ("Spec dir:",
  "spec driven", "spec-index"), default gates budget_gate + scope_gate,
  risk_class high, max_parallel_runs 1, budget caps consistent with
  `doc-to-features-loop` (100 USD total).
- `recipes/spec-driven-delivery/workflow.yaml` — nodes and edges exactly per
  the design doc pipeline: `specdir_ingest` (verifier node invoking
  `verifiers/specdir-ingest.py` which shells `bin/mini-ork specs ingest`),
  `contract_compiler` (LLM, model_lane: planner), `ratification_check`
  (verifier), `spec_lint` (verifier), `test_author` (LLM — MUST use a
  different model_lane than the implementer-role lane; use `reviewer` lane),
  `test_validity` (verifier), `per_spec_dispatcher` (implementer node whose
  prompt dispatches one inner `recursive-validate-impl` run per deliverable,
  serially, in dependency order — model the prompt on
  `recipes/doc-to-features-loop/prompts/per-feature-dispatcher.md`),
  `dispatch_aggregator` (verifier), `smoke_live` (verifier), `ui_craft_gate`
  (verifier), `ledger_writer` (verifier), `reflector`, `replanner` (recursive
  edge back to `contract_compiler`), `publisher`, `rollback`. Escalation
  edges: every verifier fail → reflector. Recursion block: max_iterations 6,
  convergence_check `all_deliverables_delivered`, divergence_kill hashing the
  failed-deliverable set as in sibling recipes.
- `recipes/spec-driven-delivery/artifact_contract.yaml` — outputs:
  `spec-index.json`, `spec-cards/<spec_id>.json`, `gates/<spec_id>.json`
  (test_author output), `ledger.jsonl`, `aggregate-verdict.json`,
  `reflector.json`, `replan.json`; success_verifiers list the real verifier
  paths; failure_policy keep evidence + replan-or-escalate.
- Prompts under `recipes/spec-driven-delivery/prompts/`:
  - `contract-compiler.md` — compile ONE source .md spec into a SpecCard JSON
    conforming to `schemas/spec-card.schema.json`; SGRM clause split; every
    acceptance criterion gets exactly one executable gate (kind cmd/ui/
    contract); deliverables atomic + dependency-ordered; forbid inventing
    requirements; emit a `ratification` section listing any source
    requirement NOT covered, never silently drop.
  - `test-author.md` — derive executable probes for every acceptance gate
    BEFORE any implementation exists; probes must fail on the untouched tree
    (broken-baseline discipline) unless tagged `precondition`; forbid vacuous
    probes (`exit 0`, grep of a string the prompt itself introduces);
    authorship separation notice: the implementer never edits these.
  - `per-spec-dispatcher.md` — walk deliverables in dependency order, write
    one child kickoff per deliverable (one deliverable per kickoff, SpecCard
    as sole anchor, gates embedded as DoD probes), dispatch inner
    `recursive-validate-impl`, record child run ids + verdicts into
    `${MINI_ORK_RUN_DIR}/dispatch-results.json`; on missing information emit
    a structured ASK artifact `${MINI_ORK_RUN_DIR}/asks/<deliverable_id>.json`
    and mark the deliverable blocked instead of guessing.
  - `reflector.md`, `replanner.md` — follow the shape of the
    `doc-to-features-loop` equivalents, operating on failed deliverables;
    replanner may ONLY mutate SpecCards via a `contract_revision` entry that
    cites gate evidence (spec is the only mutable artifact; code-side
    patch-the-patch instructions are forbidden).
- Stub verifiers under `recipes/spec-driven-delivery/verifiers/`:
  `specdir-ingest.py`, `ratification-check.py`, `spec-lint.py`,
  `test-validity.py`, `dispatch-aggregator.py`, `smoke-live.py`,
  `ui-craft-gate.py`, `ledger-writer.py` — each an executable stub printing
  `{"pass": false, "reason": "NOT_IMPLEMENTED"}` and exiting 2, with a
  module docstring stating its contract from the design doc.
- `README.md` — when to use, DAG summary, env surface
  (`MO_SDD_SPEC_GLOB`, `MO_SDD_MIN_SPECS`, `MO_SDD_UI_GATE_CMD`,
  `MO_SDD_SMOKE_TIMEOUT_S`, `MO_SDD_SURFACE_ENV`,
  `MO_SDD_MAX_PARALLEL_SPECS`), rule list from the design doc.
- `example-kickoff.md` — mirrors sibling recipes: `## Spec dir:` absolute
  path, hard rules, success section, `## Verification command`.

## Definition of Done (probes)

```bash
# P1: yaml trio parses and validates against the repo schemas
python3 -c "
import yaml, json, jsonschema, pathlib
base = pathlib.Path('recipes/spec-driven-delivery')
wf = yaml.safe_load((base/'workflow.yaml').read_text())
tc = yaml.safe_load((base/'task_class.yaml').read_text())
ac = yaml.safe_load((base/'artifact_contract.yaml').read_text())
jsonschema.validate(wf, json.loads(pathlib.Path('schemas/workflow.schema.json').read_text()))
jsonschema.validate(tc, json.loads(pathlib.Path('schemas/task_class.schema.json').read_text()))
jsonschema.validate(ac, json.loads(pathlib.Path('schemas/artifact_contract.schema.json').read_text()))
print('schemas ok')
"

# P2: every node's prompt_ref / verifier_ref resolves to an existing file
python3 -c "
import yaml, pathlib
base = pathlib.Path('recipes/spec-driven-delivery')
wf = yaml.safe_load((base/'workflow.yaml').read_text())
missing = [n['name'] for n in wf['nodes'] for k in ('prompt_ref','verifier_ref') if n.get(k) and not (base/n[k]).exists()]
assert not missing, missing
print('refs ok')
"

# P3: stub verifiers are executable and emit the stub contract
recipes/spec-driven-delivery/verifiers/spec-lint.py < /dev/null; test $? -eq 2

# P4: whole suite not broken
python3 -m pytest -q
```

## Hard rules

- Do not modify `recursive-validate-impl`, `doc-to-features-loop`, or any
  other existing recipe.
- Keep node/edge YAML style byte-consistent with sibling recipes (inline
  mapping style, same field order).
- `test_author` and the per-spec implementer path must be on different lanes
  (authorship separation) — encode in workflow.yaml, state in both prompts.
- No LLM node may be given authority to mark its own output passed; verdicts
  come only from verifier nodes.

## Success command

```bash
python3 -m pytest -q
```

## Verification command

- `python3 -c "import yaml,pathlib; yaml.safe_load(pathlib.Path('recipes/spec-driven-delivery/workflow.yaml').read_text()); print('ok')"`

## Expected outputs

- `${MINI_ORK_RUN_DIR}/implementer-summary.json`
- tier evidence logs per recursive-validate-impl contract
