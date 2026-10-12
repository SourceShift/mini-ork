# engagement-design-lens — design the engagement_check verifier

You are the **glm** lens for the orch-smoke-default-on engagement
harness. Your job is to design the `engagement_check` verifier: a
pure-python, deterministic, zero-network check that proves the
per-run orchestrator engaged by default.

## Inputs

- The orch-smoke declaration under `${MINI_ORK_ROOT}/recipes/orch-smoke/`.
- `mini_ork/orchestration/runner_orchestrator.py` — `on_node_boundary`
  and the `_emit(run_id, node_id, node_type, "orchestrator.<kind>", ...)`
  call.
- `mini_ork/observability/node_events.py` — `mo_node_emit` insert shape
  and the `run_events` columns (`event_id, run_id, event_type,
  payload_json, created_at`, plus optional `finish_reason`,
  `last_heartbeat_at`).

## Output

Write a markdown file with:

1. A first non-blank line that is a markdown header (`# …`).
2. ≥ 4 non-blank lines total.
3. No chat-transcript markers (no fenced chat blocks).
4. Sections covering:
   - **The query** — `SELECT COUNT(*) FROM run_events WHERE run_id = ?`
     AND `event_type LIKE 'orchestrator.%'` AND `json_extract(payload_json,
     '$.node_id') = 'fail_probe'`. Return `0` rows ⇒ fail; `≥ 2` rows ⇒
     fail (engagement is a single boundary per node, per the orchestrator
     runner); `= 1` row ⇒ pass.
   - **The MO_RUN_ORCHESTRATOR check** — the verifier asserts the
     variable is ABSENT from `os.environ`. A present value (especially
     `=1`) does not prove the default — `enabled()` is ON for unset /
     empty / unrecognized, OFF only for explicit falsy.
   - **The sidecar** — `verifier-result-engagement-check.json` in
     `${MINI_ORK_RUN_DIR}` with `verdict`, `row_count`, `event_type`,
     and the raw `event_type` observed (so the engagement row's
     `<kind>` is captured for review).

## Hard rules

- The query MUST filter on `node_id = 'fail_probe'`. Anti-pattern:
  counting every orchestrator.<kind> row in the run passes vacuously
  on multi-node runs (the orchestrator emits one row per boundary).
- The verifier MUST fail closed: missing `MINI_ORK_DB`, missing
  `MINI_ORK_RUN_ID`, or an unparseable `state.db` is a fail, not a skip.
- The verifier MUST NOT depend on the network, the wall clock beyond
  `created_at` ordering, or any external file outside the run dir.

## Output path

Write your lens to the run dir as
`${MINI_ORK_RUN_DIR}/lens-engagement-design.md`.
