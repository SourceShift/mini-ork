# orch-smoke-default-on kickoff

Trigger phrases: `orch-smoke`, `orchestrator-smoke`, `orchestrator
default-on`, `orchestrator engagement`, `fail_probe`,
`mo_run_orchestrator`, `run_events`, `orchestrator event row`,
`failing verifier`. The three LLM researcher lenses have small
deterministic prompts; the fail_probe leg is zero-LLM, zero-network.

## Expected outcome

**The run is EXPECTED to FAIL.** That failure is the engagement signal,
not a defect. The publisher is skipped
(`rollback_strategy: keep_artifacts_discard_publish`) and the rollback
node keeps the artifacts. The engagement_check verifier's
`verifier-result-engagement-check.json` sidecar carries the proof
(`event_type`, `row_count`).

## Success criteria

- `verifier-result-fail-probe.json` shows `pass=false` (deterministic —
  fail_probe never exits 0)
- `verifier-result-engagement-check.json` shows `pass=true` with
  `row_count: 1`, `event_type: orchestrator.<kind>`, and
  `mo_run_orchestrator: <unset>`
- `state.db::run_events` has exactly one row matching
  `run_id = <this run>`, `event_type LIKE 'orchestrator.%'`,
  `json_extract(payload_json, '$.node_id') = 'fail_probe'`

## In scope

- Run the orch-smoke-default-on nodes (3 researcher lenses + fail_probe
  + engagement_check + publisher + rollback) end to end
- Write artifacts only under `${MINI_ORK_RUN_DIR}/` (the fail-probe
  sidecar, the engagement sidecar, the lens markdown files)
- Confirm the orchestrator's `on_node_boundary` emits exactly one
  `orchestrator.<kind>` row for the fail_probe boundary

## Out of scope

- Editing `recipes/orch-smoke/` — the orch_smoke declaration is
  bound verbatim; `fail-probe.py`, `workflow.yaml`, and
  `artifact_contract.yaml` must not be modified
- "Fixing" fail_probe to exit 0 — a green fail_probe voids the proof
- Setting `MO_RUN_ORCHESTRATOR=1` (or any value) — the engagement
  check asserts the variable is ABSENT from `os.environ`
- Publishing artifacts under the target working tree — outputs[] is
  exactly the run-dir sidecar, nothing under the repo

## Verify

After the run:

```bash
# 1. The fail-probe sidecar exists with pass=false.
test -f "${MINI_ORK_RUN_DIR}/verifier-result-fail-probe.json" \
  && jq -e '.pass == false' "${MINI_ORK_RUN_DIR}/verifier-result-fail-probe.json"

# 2. The engagement sidecar exists with pass=true, row_count=1.
test -f "${MINI_ORK_RUN_DIR}/verifier-result-engagement-check.json" \
  && jq -e '.pass == true and .row_count == 1 and .event_type | startswith("orchestrator.")' \
       "${MINI_ORK_RUN_DIR}/verifier-result-engagement-check.json"

# 3. run_events has exactly one orchestrator row for fail_probe.
jq -e '
  [.[] | select(.event_type | startswith("orchestrator."))
        | select(.payload_json | fromjson | .node_id == "fail_probe")] | length == 1
' "${MINI_ORK_RUN_DIR}/run_events.jsonl"
```

## Why this exists

This recipe is the **engagement proof for the per-run orchestrator**.
After any change to `mini_ork/orchestration/runner_orchestrator.py`,
`mini_ork/observability/node_events.py`, the `enabled()` gate, or
`on_node_boundary`, re-run this recipe (with `MO_RUN_ORCHESTRATOR`
UNSET) to confirm:

- The orchestrator's `enabled()` returns True without the variable
  being set (the default-on claim)
- `on_node_boundary` emits exactly one `orchestrator.<kind>` row per
  failing verifier boundary
- The row lands in `run_events` through the canonical `mo_node_emit`
  writer (not a side channel)
- `state.db` survives the run and the engagement row is queryable
  by `run_id + node_id`

## Cost and wall time

- 3 small LLM researcher lenses: ~$0.40 each (budget_gate)
- 1 zero-LLM failing verifier: $0.00
- 1 zero-LLM engagement_check: $0.00
- Total: ~$1.20, ~3-7 minutes wall
