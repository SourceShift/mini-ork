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
| `MO_SDD_UI_PROBE_CMD` | empty | Template that runs `kind: ui` smoke probes (`{probe}`, `{spec_id}`, `{gate_id}`, `{run_dir}`). When empty, ui gates are WAIVED. |
| `MO_SDD_UI_GATE_TIMEOUT_S` | `600` | Timeout for one `MO_SDD_UI_GATE_CMD` run. |
| `MO_SDD_INGEST_TIMEOUT_S` | `300` | Timeout for the `specs ingest` call in `specdir_ingest`. |
| `MO_SDD_VAGUE_TERMS` | built-in list | Comma-separated terms `VAGUE_CRITERIA` flags in acceptance sections. |
| `MO_SDD_SPEC_MAX_BYTES` | `262144` | Spec size above which lint warns `OVERSIZE`. |
| `MO_SDD_MAX_PARALLEL_SPECS` | `1` | Reserved; not read yet. Dispatch is serial (`max_parallel_runs: 1` in `task_class.yaml`). |

## Run it

Prerequisites: the `planner`, `reviewer`, `implementer` and `worker` lanes are
configured (`config/providers.yaml` plus their keys), because
`contract_compiler`, `test_author`, `per_spec_dispatcher` and the
`recursive-validate-impl` child runs dispatch to them.

1. Write the kickoff. Put a `## Spec dir:` heading in it, with the spec
   directory as a backticked path on the next non-empty line (or inline after
   the colon). A relative path resolves against the target repo.
   `example-kickoff.md` is a template.

   ```markdown
   ## Spec dir:

   `/absolute/path/to/specs`
   ```

   `specdir_ingest` reads the first kickoff it finds, in this order:
   `MINI_ORK_KICKOFF`, `MINI_ORK_KICKOFF_PATH` (a variable that is set must
   name an existing file), `${MINI_ORK_RUN_DIR}/kickoff.md`, then
   `kickoff_path` from `${MINI_ORK_RUN_DIR}/run_profile.json`. A missing
   kickoff, a missing `## Spec dir:` line, or a spec dir that does not exist
   exits 2.

2. Lint the specs before spending budget. An error finding (`NO_ACCEPTANCE`,
   `DUP_ID`, `DEP_CYCLE`) fails `spec_lint` in the run, too.

   ```bash
   bin/mini-ork specs lint <spec_dir>
   ```

3. Launch the run.

   ```bash
   bin/mini-ork run spec-driven-delivery kickoffs/<your-kickoff>.md
   ```

Run-level variables (the recipe's `MO_SDD_*` knobs are in
[Environment](#environment)):

| Variable | Effect |
|---|---|
| `MINI_ORK_ROOT` | Engine checkout the launcher and verifiers import from (`MINI_ORK_ENGINE_ROOT` wins when both are set). |
| `MINI_ORK_HOME` | State directory (`.mini-ork/`); run artifacts land in `$MINI_ORK_HOME/runs/<run_id>/`. |
| `MINI_ORK_RUN_ID` | Pin the run id instead of generating one. |
| `MO_TARGET_CWD` | Repo the nodes work in and the verifiers run in, when it is not the directory you launch from. |
| `MINI_ORK_VENV` | Virtualenv the launcher re-execs into (default: `.mini-ork/runtime-python`, else the checkout's `.venv`). |
| `MO_NODE_TIMEOUT_S` | Per-node timeout for LLM nodes, in seconds (default `1500`). |
| `MINI_ORK_KICKOFF`, `MINI_ORK_KICKOFF_PATH` | Point `specdir_ingest` at a kickoff other than the run's copy. |

Check the deterministic spine without any LLM lane:

```bash
python3 -m pytest -q tests/test_sdd_e2e_dryrun.py
```

It copies `tests/fixtures/sdd_e2e` (a target repo with `add()` and a spec for
`multiply()`) to a temp dir. Then it runs the real `specs ingest` and
`specs lint` and every verifier in workflow order: gates fail before
`multiply()` lands, smoke is red, then green after it lands, and the ledger
gets one row per gate. The SpecCard, gates and child run that the LLM nodes
would write are planted from the fixture's golden card.

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

- K2: scaffold (DAG, prompts, artifact contract).
- K3: all eight `verifiers/*.py` are live deterministic gates, unit-tested in
  `tests/test_sdd_verifiers.py`.
- K4: `tests/test_sdd_e2e_dryrun.py` runs the deterministic spine end to end
  on the `tests/fixtures/sdd_e2e` fixture. The LLM nodes (`contract_compiler`,
  `test_author`, `per_spec_dispatcher`) are not exercised there; their output
  is planted.
