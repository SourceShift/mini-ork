# Gap S1: exit-code rewards are labelled provisional, and grounding is per node

## Goal

Every trace is stamped at write time from the node's exit status:
`_make_trace_fn` (`mini_ork/cli/execute.py` ~4509, ~4582) calls
`reward_from_status(status, verdict)` and never sets `reward_source`, so the
row inherits the default `verifier@v1` (`mini_ork/trace_store.py:145`). The
label claims verifier evidence that does not exist; the verifiers run later in
the DAG. Stage traces take the same path (`enrich_stage_trace`,
`trace_store.py:288`). Only `mini_ork/cli/verify.py` (~713-732) writes a trace
with a real verdict.

Post-run, `stamp_artifact_rewards` (`mini_ork/learning/writeback.py:616`)
re-stamps implementer traces with the pass rate of the run's
`verifier_results` rows in the trace's attempt window (`artifact_window_counts`,
:572). Two problems:

1. **Windows are per node TYPE.** The i-th implementer trace's window ends at
   the next implementer trace of any node, so parallel implementer nodes cut
   each other's windows to seconds.
2. **Only implementers are grounded** (`ARTIFACT_REWARD_NODE_TYPES = ("implementer",)`,
   :36). Lens, planner and reviewer traces keep a 1.0 exit-code reward forever
   (live DB, 30 days: researcher 1,331 of 1,343 rows at ≥0.99).

Run-scoped verifier evidence judges the artifact, not each upstream node, so
widening grounding beyond the implementer is a credit-assignment choice; it
ships behind a flag, off.

All paths are relative to the repository root.

## Mechanism (exact spec)

### 1. Label the provisional reward

- `mini_ork/cli/execute.py`, in the trace function (~4509-4590): add
  `"node_id": node_id` to the `verifier_output` dict (keep `node_type` and
  `finish_reason`; consumers group on `node_type`), and set
  `reward_source = "exit_status@v1"` and `reward_primary_metric = "exit_status"`
  on the payload it writes.
- `mini_ork/trace_store.py`, `enrich_stage_trace` (~288-324): set the same two
  fields when it computes a status-derived reward and none is set.
- `mini_ork/cli/verify.py` (~713-732): pass `reward_source = "verifier@v1"`
  explicitly (its verdict is real).
- The `trace_store.py:145` default is unchanged (legacy writers).

### 2. `mini_ork/learning/writeback.py`

- `_EXIT_CODE_REWARD_SOURCES` also contains `"exit_status@v1"`, so the re-stamp
  still applies to the new label.
- **Per-node windows.** `stamp_artifact_rewards` orders eligible traces by
  `created_at, trace_id` and bounds each trace's window by the next trace of
  the SAME `node_id` (read `$.node_id` from `verifier_output`; for legacy rows
  without it, parse `node_id` from `trace_id` `tr-<node_type>-<node_id>-<hex8>`;
  unparseable → fall back to today's per-type window). The last trace of a
  node stays open-ended.
- **Node types.** `ARTIFACT_REWARD_NODE_TYPES` stays `("implementer",)` as the
  default. `MO_ARTIFACT_REWARD_NODE_TYPES` (comma-separated) overrides it;
  `verifier` is always excluded; an empty or unusable value falls back to the
  default with one stderr line.
- Everything else (eligibility: success + valid + exit-code source, the
  untouched-when-no-evidence rule, idempotency, never raising) is unchanged.

## Files in scope (touch ONLY these)

- `mini_ork/cli/execute.py`
- `mini_ork/trace_store.py`
- `mini_ork/cli/verify.py`
- `mini_ork/learning/writeback.py`
- `tests/unit/test_artifact_node_reward.py`
- `tests/unit/test_mini_ork_execute_py.py`
- `tests/unit/test_stage_reward_coverage_py.py`

Do not modify `mini_ork/dispatch/lane_router.py`, `mini_ork/learning/advantage_store.py`, `mini_ork/cli/execute_handlers.py`, `docs/`, any migration, or any path in `config/kernel-frozen.yaml`. Tests that pin the old `verifier@v1` label for exit-code traces (`tests/unit/test_artifact_node_reward.py` ~153 and ~248, `tests/unit/test_mini_ork_execute_py.py` trace_fn cases ~1466-1621) may be updated to the new label; every other existing test must pass unchanged.

## Tests (hermetic)

In `tests/unit/test_artifact_node_reward.py`:

1. Two implementer nodes (`impl_a`, `impl_b`) whose traces start 2 s apart,
   with a failing verifier row 10 s after `impl_a` and a passing one 20 s after
   `impl_b` → each trace is grounded on its own node's window (`impl_a` gets the
   fail; `impl_b` the pass), not cut short by the other node's trace.
2. A legacy trace with no `node_id` in `verifier_output` and trace_id
   `tr-implementer-impl_a-1a2b3c4d` is windowed by node `impl_a`.
3. `MO_ARTIFACT_REWARD_NODE_TYPES=implementer,researcher` re-stamps a
   researcher trace; unset → it is left untouched; `verifier` in the list is
   ignored.
4. An `exit_status@v1` trace is eligible for re-stamping.

In `tests/unit/test_mini_ork_execute_py.py` (trace_fn cases):

5. A trace written by the trace function carries
   `reward_source == "exit_status@v1"`, `reward_primary_metric == "exit_status"`
   and `verifier_output.node_id`.

In `tests/unit/test_stage_reward_coverage_py.py`:

6. A stage trace enriched from a status gets `reward_source == "exit_status@v1"`.

## Verification commands

- `python3.11 -m pytest -q -p no:asyncio tests/unit/test_artifact_node_reward.py tests/unit/test_mini_ork_execute_py.py tests/unit/test_stage_reward_coverage_py.py tests/unit/test_verify_partial_trace.py tests/unit/test_verifier_results_wire.py tests/unit/test_impl_run_verified_reward.py tests/unit/test_trace_store_py.py tests/unit/test_writeback_validity_masking_py.py`

## Self-application measurement

```measure
python3.11 -m pytest -q -p no:asyncio tests/unit/test_artifact_node_reward.py --junitxml="${MINI_ORK_RUN_DIR}/gap-s1-junit.xml"
```

## Required artifacts — declare EXACTLY these and nothing else

- `${MINI_ORK_RUN_DIR}/gap-s1-junit.xml`

## Success criteria

- The verification command passes; every pre-existing test not listed above is unchanged.
- `ruff check --select F,E9 mini_ork/cli/execute.py mini_ork/trace_store.py mini_ork/cli/verify.py mini_ork/learning/writeback.py tests/unit/test_artifact_node_reward.py tests/unit/test_mini_ork_execute_py.py tests/unit/test_stage_reward_coverage_py.py` is clean.
- No trace claims `verifier@v1` unless a verifier produced its reward.
