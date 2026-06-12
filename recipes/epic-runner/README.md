# epic-runner

Multi-epic delivery orchestrator for mini-ork.

It ingests one markdown document describing a directed acyclic graph of epics,
computes topological waves, dispatches each epic as a child `framework-edit`
run, aggregates the per-epic verdicts, and emits a single gated delivery report.

## Topology

```text
planner
  → epic_dispatcher
  → wave_aggregator

epic_dispatcher → wave_aggregator  (raw per-epic results)
wave_aggregator → epic_verifier
epic_verifier   → final_reviewer
final_reviewer  → publisher
final_reviewer  → rollback (on failure)
epic_verifier   → rollback (on failure)
reflector runs after the delivery is decided
```

The recipe has exactly 8 nodes:

1. `planner`
2. `epic_dispatcher`
3. `wave_aggregator`
4. `epic_verifier`
5. `final_reviewer`
6. `publisher`
7. `rollback`
8. `reflector`

## Model Lanes

The LLM-dispatching lanes intentionally span three model families:

- `epic_dispatcher`: `codex_lens`
- `wave_aggregator`: `glm_lens`
- `final_reviewer`: `opus_lens`

`planner`, `verifier`, `publisher`, `rollback`, and `reflector` are operational
lanes and are not counted as research-family diversity.

## Environment Variables

The recipe reads these environment variables:

- `MINI_ORK_EPIC_DOC` — path to the multi-epic markdown document.
- `MINI_ORK_EPIC_TARGET_REPO` — target repository the child epics mutate.
- `MINI_ORK_EPIC_PUBLISH` — `true`/`false`; whether child framework-edit runs
  may publish.
- `MINI_ORK_EPIC_VERIFIER_SCRIPT` — optional operator-supplied verifier script
  path.

## Artifacts

Run-local artifacts:

- `${MINI_ORK_RUN_DIR}/epic-runner-plan.json`
- `${MINI_ORK_RUN_DIR}/epic-results.json`
- `${MINI_ORK_RUN_DIR}/wave-aggregate.json`
- `${MINI_ORK_RUN_DIR}/verifier-epic-graph-complete.json`
- `${MINI_ORK_RUN_DIR}/review-final-reviewer.json`
- `${MINI_ORK_RUN_DIR}/epic-runner-delivery.json`
- `${MINI_ORK_RUN_DIR}/reflection-epic-runner.md`

`epic-runner-delivery.json` uses:

```json
{
  "pass": false,
  "epics_total": 0,
  "epics_passed": 0,
  "waves_total": 0,
  "final_artifact_ref": ""
}
```

## Dispatcher↔Aggregator Loop

The recipe needs a feedback loop between the dispatcher (which spawns child
runs) and the aggregator (which decides when a wave is complete). Because the
workflow YAML schema disallows cycles, this loop is emulated inside the
`epic_dispatcher` node via internal polling. The dispatcher writes raw results
and only hands control to `wave_aggregator` after all waves are finished.

## Verifiers

`verifiers/epic-graph-complete.sh` is the deterministic gate. It checks:

- All required JSON artifacts exist and parse.
- Every epic declared in the plan appears in the results.
- The aggregate reports `dependency_respected: true`.

This drafter candidate ships a stub verifier script only. The downstream
selected recipe should replace it with full checks before publication.

## Rollback Strategy

`rollback_strategy: keep_run_artifacts_discard_partial_publish`.

On verifier or reviewer failure, the recipe preserves run-local evidence and
removes any partially published state in the target repo. It does not touch
child run directories or unrelated source files.

## Failure-Mode Coverage

The verifier contracts will eventually cover:

- Missing or malformed plan/results/aggregate JSON.
- Epics declared in the plan but absent from results.
- Dependency violations (a downstream epic passed while an upstream epic did
  not).
- Waves dispatched out of order.
- Child runs that reported success but left `files_written` empty.
- Non-deterministic verifier behavior (timestamps, temp paths, unsorted output).
